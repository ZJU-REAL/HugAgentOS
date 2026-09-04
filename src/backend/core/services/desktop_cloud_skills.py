"""桌面双端：云端技能同步到本机（云端为真源，本机只保存文件快照）。

桥激活后，本模块随 MCP manifest 的同一轮询、同一枚 capability token 拉取
云端技能清单（``/v1/desktop/capability/skills/manifest``），把清单里每个技能
的完整 zip 包落到 ``get_cloud_skills_dir()/<skill_id>/``，并镜像一份到共享
沙箱技能目录，使其在沙箱里同样出现在 ``/workspace/skills/<skill_id>``。

- 云端技能目录以最高优先级注册为技能来源（见 ``agent_skills.config``），
  同 id 的本机内置 / 本机库技能被云端版本覆盖；
- 启用清单以云端为准：云端可见但当前停用的 id（``suppressed_ids``）在本机
  一并停掉，云端启用的 id 追加到清单尾部；本机独有的技能原样保留；
- 云端断线时保留上一份快照继续可用；切换账号时整目录清空。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INDEX_NAME = ".cloud_index.json"

_lock = threading.Lock()
_sync_lock = threading.Lock()  # one reconcile at a time: staging dirs and index writes
_manifest: Optional[Dict[str, Any]] = None
_index: Optional[Dict[str, str]] = None  # skill_id → content_hash of the files on disk
_error: Optional[str] = None


def _root() -> Path:
    from core.agent_skills.config import get_cloud_skills_dir

    return get_cloud_skills_dir()


def _load_index(root: Path) -> Dict[str, str]:
    try:
        raw = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
        return {
            str(k): str(v)
            for k, v in (raw or {}).items()
            if isinstance(k, str) and isinstance(v, str) and (root / k / "SKILL.md").is_file()
        }
    except Exception:  # noqa: BLE001 - a missing/corrupt index means "nothing installed"
        return {}


def _save_index(root: Path, index: Dict[str, str]) -> None:
    (root / INDEX_NAME).write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )


def _current_index() -> Dict[str, str]:
    global _index
    with _lock:
        if _index is None:
            _index = _load_index(_root())
        return dict(_index)


def _safe_skill_dir(root: Path, skill_id: str) -> Optional[Path]:
    name = (skill_id or "").strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name or name in (".", ".."):
        return None
    return root / name


def _write_bundle(root: Path, skill_id: str, data: bytes) -> None:
    from core.agent_skills.binary_files import decode_binary, is_binary_value
    from core.services.marketplace_service import parse_skill_zip

    target = _safe_skill_dir(root, skill_id)
    if target is None:
        raise ValueError(f"unsafe skill id: {skill_id!r}")
    parsed = parse_skill_zip(data)
    staging = root / f".{skill_id}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_text(str(parsed.get("skill_content") or ""), encoding="utf-8")
    for rel, body in (parsed.get("extra_files") or {}).items():
        parts = Path(str(rel)).parts
        if not parts or ".." in parts or Path(str(rel)).is_absolute():
            continue
        path = staging.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_binary_value(body):
            path.write_bytes(decode_binary(body))
        else:
            path.write_text(str(body), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)


def _mirror(root: Path, skill_id: str) -> None:
    """Copy the cloud snapshot into the shared sandbox dir (what sandboxes mount)."""
    from core.agent_skills.config import get_sandbox_skills_dir

    src = _safe_skill_dir(root, skill_id)
    if src is None or not src.is_dir():
        return
    dest = get_sandbox_skills_dir() / skill_id
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)


def _unmirror(skill_id: str) -> None:
    """Drop the shared copy; a same-id built-in bundle takes the slot back."""
    from core.agent_skills.config import _builtin_skills_dir, get_sandbox_skills_dir

    dest = get_sandbox_skills_dir() / skill_id
    shutil.rmtree(dest, ignore_errors=True)
    builtin = _builtin_skills_dir() / skill_id
    if builtin.is_dir():
        shutil.copytree(
            builtin, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
        )


def _remove(root: Path, skill_id: str) -> None:
    target = _safe_skill_dir(root, skill_id)
    if target is not None:
        shutil.rmtree(target, ignore_errors=True)
    _unmirror(skill_id)


def _reconcile(
    manifest: Dict[str, Any], cloud_base: str, headers: Dict[str, str], *, full_mirror: bool
) -> bool:
    """Bring local files in line with ``manifest``; returns whether anything changed."""
    global _index
    import httpx
    from core.agent_skills.config import get_sandbox_skills_dir

    root = _root()
    shared = get_sandbox_skills_dir()
    index = _current_index()
    wanted = {s["skill_id"]: s["content_hash"] for s in manifest["skills"]}
    changed = False

    for sid in [sid for sid in index if sid not in wanted]:
        _remove(root, sid)
        index.pop(sid, None)
        changed = True

    for sid, content_hash in wanted.items():
        if index.get(sid) == content_hash and (root / sid / "SKILL.md").is_file():
            # Startup re-syncs built-ins over the shared dir and prunes unknown
            # dirs there, so the mirror is re-asserted on every full pass.
            if full_mirror or not (shared / sid / "SKILL.md").is_file():
                _mirror(root, sid)
            continue
        try:
            resp = httpx.get(
                f"{cloud_base}/api/v1/desktop/capability/skills/{sid}/bundle",
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=5.0),
            )
            resp.raise_for_status()
            _write_bundle(root, sid, resp.content)
            index[sid] = resp.headers.get("etag", "").strip().strip('"') or content_hash
            _mirror(root, sid)
            changed = True
            logger.info("[cloud-skills] installed '%s' (%d bytes)", sid, len(resp.content))
        except Exception as exc:  # noqa: BLE001 - one bad skill must not block the rest
            logger.warning("[cloud-skills] install '%s' failed: %s", sid, exc)
            if index.pop(sid, None) is not None:
                changed = True

    _save_index(root, index)
    with _lock:
        _index = dict(index)
    return changed


def sync_blocking(state: Dict[str, Any]) -> None:
    """Fetch the cloud skill manifest and reconcile local files (background thread only)."""
    global _manifest, _error
    import httpx
    from core.services.desktop_capability_protocol import validate_skill_manifest

    cloud_base = str(state["cloud_base"])
    headers = {"Authorization": f"Bearer {state['token']}"}
    with _sync_lock:
        with _lock:
            current = copy.deepcopy(_manifest)
        request_headers = dict(headers)
        if current:
            request_headers["If-None-Match"] = f'"{current["revision"]}"'
        try:
            resp = httpx.get(
                f"{cloud_base}/api/v1/desktop/capability/skills/manifest",
                headers=request_headers,
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            if resp.status_code == 304 and current:
                manifest = current
                full_mirror = False
            else:
                resp.raise_for_status()
                body = resp.json()
                manifest = validate_skill_manifest(
                    body.get("data") if isinstance(body, dict) else None
                )
                full_mirror = True
            changed = _reconcile(manifest, cloud_base, headers, full_mirror=full_mirror)
            with _lock:
                _manifest = manifest
                _error = None
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _error = str(exc)
            logger.warning("[cloud-skills] sync failed: %s", exc)
            return
    if changed:
        from core.agent_skills.cache_refresh import refresh_skill_caches

        refresh_skill_caches()
        logger.info(
            "[cloud-skills] synced revision=%s skills=%d",
            manifest["revision"][:12],
            len(manifest["skills"]),
        )


def apply_to_enabled_skill_ids(skill_ids: List[str]) -> List[str]:
    """Cloud-enabled skills replace the local view; cloud-disabled ids are removed too."""
    with _lock:
        manifest = _manifest
    if not manifest:
        return skill_ids
    installed = _current_index()
    cloud_ids = [s["skill_id"] for s in manifest["skills"] if s["skill_id"] in installed]
    hidden = set(manifest["suppressed_ids"]) | set(cloud_ids)
    kept = [sid for sid in skill_ids if sid not in hidden]
    return kept + cloud_ids


def purge_all() -> None:
    """Remove every synced skill (account switch); the next sync repopulates."""
    global _manifest, _index, _error
    root = _root()
    for sid in _current_index():
        _remove(root, sid)
    _save_index(root, {})
    with _lock:
        _manifest = None
        _index = {}
        _error = None


def status() -> Dict[str, Any]:
    with _lock:
        manifest = _manifest
        err = _error
    installed = _current_index()
    return {
        "revision": str((manifest or {}).get("revision") or ""),
        "cloud_skill_count": len((manifest or {}).get("skills") or []),
        "installed_count": len(installed),
        "suppressed_count": len((manifest or {}).get("suppressed_ids") or []),
        "skills": [
            {
                "skill_id": s["skill_id"],
                "display_name": s["display_name"],
                "scope": s["scope"],
                "installed": s["skill_id"] in installed,
            }
            for s in ((manifest or {}).get("skills") or [])
        ],
        "last_error": err,
    }


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    global _manifest, _index, _error
    with _lock:
        _manifest = None
        _index = None
        _error = None
