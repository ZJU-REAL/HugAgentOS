"""桌面双端：云端技能清单同步 + 按需准备（存储层 / 安装索引 / 运行视图）。

桥激活后，本模块随 MCP manifest 的同一轮询、同一枚 capability token 拉取
云端技能清单（``/v1/desktop/capability/skills/manifest``），把它写成**账号
安装意图**（``device_capability_installations``，profile = 当前云端账号）：

- 清单里尚未就绪的技能在同一轮同步里直接下载准备好——登录完成时账号的技能
  就已经可用，界面上没有任何「待下载 / 在本机准备」的手动步骤；
- 本机已就绪（ready）而云端内容哈希变了的技能自动拉新版本到新的 revision
  目录并切换视图；
- 云端停用（``suppressed_ids``）的技能在本机置为停用，文件保留；
- 从清单消失的技能撤销运行授权；历史 revision 留给运行记录与恢复。

下载的包先落 staging，解压后按同一哈希算法核对内容，再以 ``os.replace`` 发布
成不可变 revision；视图联接在索引提交后重建。切换账号不删文件——另一个账号
的 profile 目录本就隔离，只重建当前用户的视图。
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Dict, List, Optional

from core.capabilities import registry, skills, store, manifest_order
from core.capabilities.errors import CloudUnavailable, IntegrityFailed, PackageMissing
from core.capabilities.paths import KIND_SKILL, capabilities_enabled, revision_for_hash
from core.capabilities.ref import cloud_ref, profile_id

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_sync_lock = threading.Lock()  # one reconcile at a time
_manifest: Optional[Dict[str, Any]] = None
_error: Optional[str] = None


def _profile(state: Dict[str, Any]) -> str:
    from core.services.desktop_capability_protocol import token_subject

    subject = token_subject(str(state.get("token") or ""))
    if not subject:
        raise CloudUnavailable("capability token carries no subject; cannot map an account profile")
    return profile_id(str(state["cloud_base"]), subject)


def _headers(state: Dict[str, Any]) -> Dict[str, str]:
    from core.services.desktop_cloud_bridge import cloud_headers
    return cloud_headers(state)


def _fetch_manifest(state: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    from core.services.desktop_capability_protocol import validate_skill_manifest

    ticket = manifest_order.begin(KIND_SKILL, _profile(state))
    with _lock:
        current = copy.deepcopy(_manifest)
    headers = _headers(state)
    if current:
        headers["If-None-Match"] = f'"{current["revision"]}"'
    resp = httpx.get(
        f"{state['cloud_base']}/api/v1/desktop/capability/skills/manifest",
        headers=headers,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    if resp.status_code == 304 and current:
        return manifest_order.stamp(current, ticket)
    resp.raise_for_status()
    body = resp.json()
    return manifest_order.stamp(validate_skill_manifest(body.get("data") if isinstance(body, dict) else None), ticket)


def _reconcile_intent(manifest: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    """Atomically gate intent/cache publication against newer in-flight requests."""
    global _manifest, _error
    from core.services.desktop_cloud_bridge import account_scope

    with account_scope(state):
        with manifest_order.apply(KIND_SKILL, _profile(state), manifest):
            updates = _apply_intent(manifest, state)
            with _lock:
                _manifest, _error = manifest, None
            return updates


def _apply_intent(manifest: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    """Write current intent while the account and manifest publication locks are held."""
    profile = _profile(state)
    cloud_base = str(state["cloud_base"])
    wanted = {s["skill_id"]: s for s in manifest["skills"]}
    suppressed = set(manifest["suppressed_ids"])
    existing = {
        inst.key: inst
        for inst in registry.list_installations(kind=KIND_SKILL, profile_id=profile, include_removed=True)
    }
    needs_prepare: List[str] = []
    for sid, entry in wanted.items():
        before = existing.get(sid)
        inst = registry.upsert(
            profile_id=profile,
            ref=cloud_ref(cloud_base, KIND_SKILL, sid, scope=entry["scope"]),
            display_name=entry["display_name"],
            description=entry["description"],
            version=entry["version"],
            content_hash=entry["content_hash"],
            source="cloud",
            payload={"scope": entry["scope"], "mcp_server_ids": list(entry["mcp_server_ids"])},
            enabled=True,
        )
        if not inst.ready:
            needs_prepare.append(inst.install_id)
        elif before is not None and before.content_hash != entry["content_hash"]:
            needs_prepare.append(inst.install_id)
        elif inst.payload.get("update_available"):
            needs_prepare.append(inst.install_id)
    for sid, inst in existing.items():
        if sid in wanted:
            continue
        if sid in suppressed:
            if inst.state != "removed":
                registry.set_state(inst.install_id, inst.state, payload_update={"source_enabled": False})
                registry.set_enabled(inst.install_id, False)
            continue
        if inst.state != "removed":
            registry.mark_removed(inst.install_id)
    return needs_prepare


def sync_blocking(state: Dict[str, Any]) -> None:
    """Fetch the cloud skill manifest and reconcile intent + views (background thread only)."""
    global _manifest, _error
    if not capabilities_enabled():
        with _lock:
            _error = "capability store disabled (HUGAGENT_CAPS_ROOT unset)"
        return
    with _sync_lock:
        try:
            from core.services.desktop_cloud_bridge import account_scope

            manifest = _fetch_manifest(state)
            with account_scope(state):
                with manifest_order.apply(KIND_SKILL, _profile(state), manifest):
                    with _lock:
                        changed = _manifest is None or _manifest["revision"] != manifest["revision"]
                    pending = _reconcile_intent(manifest, state)
        except manifest_order.StaleManifest:
            return
        except Exception as exc:  # noqa: BLE001
            from core.services.desktop_cloud_bridge import _state_fingerprint, get_state

            if _state_fingerprint(get_state()) != _state_fingerprint(state):
                return
            with _lock:
                _error = str(exc)
            logger.warning("[cloud-skills] sync failed: %s", exc)
            return
        prepared = prepare(state, pending) if pending else []
    if changed or prepared:
        skills.bump_view_generation()
        skills.rebuild_views(None)
        from core.agent_skills.cache_refresh import refresh_skill_caches

        refresh_skill_caches()
        logger.info(
            "[cloud-skills] synced revision=%s skills=%d prepared=%d",
            manifest["revision"][:12],
            len(manifest["skills"]),
            len(prepared),
        )


def _download(state: Dict[str, Any], skill_id: str) -> bytes:
    import httpx

    try:
        resp = httpx.get(
            f"{state['cloud_base']}/api/v1/desktop/capability/skills/{skill_id}/bundle",
            headers=_headers(state),
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
    except httpx.HTTPError as exc:
        raise CloudUnavailable(f"bundle download failed: {exc}", ref=skill_id) from exc
    if resp.status_code == 404:
        raise PackageMissing("cloud no longer offers this skill", ref=skill_id)
    if resp.status_code >= 400:
        raise CloudUnavailable(f"bundle download HTTP {resp.status_code}", ref=skill_id)
    return resp.content


def prepare_one(state: Dict[str, Any], install_id: str) -> Dict[str, Any]:
    """Publish verified bytes for this account; failed updates keep the old revision."""
    from core.capabilities.preparation import prepare_component

    inst = registry.get(install_id)
    if inst is None or inst.kind != KIND_SKILL:
        raise PackageMissing("unknown skill installation", ref=install_id)
    return prepare_component(
        state, inst,
        download=lambda: _download(state, inst.key),
        content_hash=lambda comp: skills.skill_dir_hash(comp.path, fresh=True),
    ).to_dict()


def prepare(state: Dict[str, Any], install_ids: List[str]) -> List[Dict[str, Any]]:
    """Prepare several installations; one failure never blocks the others."""
    results: List[Dict[str, Any]] = []
    for iid in install_ids:
        try:
            results.append({"install_id": iid, "ok": True, "installation": prepare_one(state, iid)})
        except Exception as exc:  # noqa: BLE001
            error = exc.to_dict() if hasattr(exc, "to_dict") else {"code": "prepare_failed", "message": str(exc)}
            results.append({"install_id": iid, "ok": False, "error": error})
            logger.warning("[cloud-skills] prepare '%s' failed: %s", iid, exc)
    if any(r["ok"] for r in results):
        skills.bump_view_generation()
        skills.rebuild_views(None)
        from core.agent_skills.cache_refresh import refresh_skill_caches

        refresh_skill_caches()
    return results


def apply_to_enabled_skill_ids(skill_ids: List[str]) -> List[str]:
    """Merge this account's ready skills into the run's list; drop suppressed and conflicted names."""
    with _lock:
        manifest = _manifest
    if not manifest:
        return skill_ids
    resolution = skills.resolve_for_user(skills.current_local_user_id())
    from core.capabilities.plugins import enabled_cloud_skill_intents

    plugin_intents = enabled_cloud_skill_intents(skills.current_local_user_id())
    hidden = (
        set(manifest["suppressed_ids"])
        | set(resolution.conflicts)
        | (set(resolution.unusable) - plugin_intents)
    )
    ready = [c.runtime_name for c in resolution.chosen.values() if c.source == "cloud"]
    kept = [sid for sid in skill_ids if sid not in hidden]
    seen = set(kept)
    return kept + [
        sid for sid in sorted(set(ready) | plugin_intents) if sid not in seen and sid not in hidden
    ]


def on_account_switch() -> None:
    """A different cloud account is now bridged: forget the old manifest, keep its files."""
    global _manifest, _error
    with _lock:
        _manifest = None
        _error = None
    skills.bump_view_generation()


def status() -> Dict[str, Any]:
    with _lock:
        manifest = _manifest
        err = _error
    from core.services.desktop_cloud_bridge import get_state

    st = get_state()
    profile = None
    installations: List[Dict[str, Any]] = []
    if st and capabilities_enabled():
        try:
            profile = _profile(st)
            installations = [
                inst.to_dict() for inst in registry.list_installations(kind=KIND_SKILL, profile_id=profile)
            ]
        except CloudUnavailable as exc:
            err = err or str(exc)
    return {
        "revision": str((manifest or {}).get("revision") or ""),
        "profile_id": profile,
        "cloud_skill_count": len((manifest or {}).get("skills") or []),
        "installed_count": sum(1 for i in installations if i["state"] == "ready"),
        "pending_count": sum(1 for i in installations if i["state"] == "pending"),
        "suppressed_count": len((manifest or {}).get("suppressed_ids") or []),
        "installations": installations,
        "last_error": err,
    }


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    global _manifest, _error
    with _lock:
        _manifest = None
        _error = None
