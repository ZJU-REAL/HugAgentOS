"""Authorized cloud skill manifests and bundles."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.db.engine import SessionLocal
from core.services.desktop_capability_configs import _EFFECTIVE_TTL_S, _effective_lock
from core.services.desktop_capability_protocol import build_skill_manifest, skill_content_hash

# ── 技能清单 / 技能包（云端为真源，本机只缓存文件快照） ───────────────────

_SKILL_SKIP_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}
_skill_manifest_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _skill_info(skill_id: str):
    from core.agent_skills.loader import get_skill_loader

    return get_skill_loader()._backend.get_skill_info(skill_id)


def _skill_snapshot(
    skill_id: str, info=None
) -> Optional[Tuple[str, Dict[str, str], Optional[Path]]]:
    """(SKILL.md 正文, {相对路径: 内容}, 文件系统技能目录或 None)。

    ``info`` 给已经取过的调用方复用，省一次后端查询。
    """
    from core.agent_skills.binary_files import pack_directory
    from core.agent_skills.loader import get_skill_loader

    loader = get_skill_loader()
    info = _skill_info(skill_id) if info is None else info
    if info is None:
        return None
    if not info.is_database and info.content is None and info.file_path is not None:
        skill_dir = Path(info.file_path).parent
        files = {
            rel: body
            for rel, body in pack_directory(skill_dir).items()
            if not _SKILL_SKIP_PARTS.intersection(rel.split("/")) and not rel.endswith(".pyc")
        }
        return files.pop("SKILL.md", ""), files, skill_dir
    content = loader._backend.read_skill_file(skill_id) if info.is_database else info.content
    return str(content or ""), dict(loader.get_extra_files(skill_id) or {}), None


def build_user_skill_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    """这个账号拥有的技能清单（含内容哈希与云端当前启停）。

    清单回答的是「装了什么」，不是「开着什么」：库里的技能不论启停一律下发，本机才
    装得齐——插件的子技能和智能体依赖的技能在云端常是关着的，只发启用的会让它们在
    本机整片缺失，插件下方空无一物、智能体调用时找不到工具。``enabled`` 只作本机
    首次落地的初值，之后启停由本机自己控制。

    内置技能随本机后端一起分发，本机目录里本来就有，仍按启用集下发。
    """
    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _skill_manifest_cache.get(uid)
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])

    from core.agent_skills.loader import get_skill_loader
    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.llm.factory.selection.capabilities import _filter_skill_ids_for_user

    with SessionLocal() as db:
        enabled, _agents, _mcps = resolve_all_runtime_enabled(db, uid)
    enabled_ids = set(_filter_skill_ids_for_user(list(enabled or []), uid))
    loader = get_skill_loader()
    metadata = loader.load_all_metadata()
    visible = sorted(sid for sid in metadata if loader.get_skill_owner(sid) in (None, uid))

    skills: List[Dict[str, Any]] = []
    for sid in visible:
        meta = metadata.get(sid)
        info = _skill_info(sid) if meta is not None else None
        if info is None:
            continue
        if sid not in enabled_ids and not info.is_database:
            continue
        snapshot = _skill_snapshot(sid, info)
        if snapshot is None:
            continue
        content, files, _dir = snapshot
        skills.append(
            {
                "skill_id": sid,
                "display_name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "scope": "private" if loader.get_skill_owner(sid) else "shared",
                "content_hash": skill_content_hash(content, files),
                "mcp_server_ids": list(meta.mcp_server_ids or []),
                "enabled": sid in enabled_ids,
                "source_plugin": str(getattr(info, "source_plugin", "") or ""),
            }
        )
    manifest = build_skill_manifest(skills)
    with _effective_lock:
        _skill_manifest_cache[uid] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_skill_bundle(user_id: str, skill_id: str) -> Optional[Tuple[bytes, str]]:
    """打包一个当前授权技能为 zip，返回 (bytes, content_hash)；未授权返回 None。"""
    from core.services.marketplace_service import build_skill_zip, build_skill_zip_from_dir

    manifest = build_user_skill_manifest(user_id, use_cache=False)
    if not any(s["skill_id"] == skill_id for s in manifest["skills"]):
        return None
    snapshot = _skill_snapshot(skill_id)
    if snapshot is None:
        return None
    content, files, skill_dir = snapshot
    if skill_dir is not None:
        data = build_skill_zip_from_dir(skill_id, skill_dir)
    else:
        data = build_skill_zip(skill_id, content, files)
    return data, skill_content_hash(content, files)
