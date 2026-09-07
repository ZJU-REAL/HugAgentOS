"""本机能力安装接口（桌面双端的本机后端侧）。

  GET  /v1/desktop/capabilities/installations      账号意图 + 设备状态 + 本轮解析结果
  POST /v1/desktop/capabilities/sync               同步云端清单并把缺的文件准备好
  POST /v1/desktop/capabilities/views/rebuild      重建运行视图
  GET  /v1/desktop/capabilities/mcp-json           mcp.json 投影（只读诊断，不含密钥）

本机能力没有任何手动管理动作：登录时同步一次就全部就绪，云端能力被改动时前端
调一次 ``/sync``。界面上只标注能力来自本机还是云端。

全部端点只在「桌面壳孵化、且配置了能力文件存储」的本机后端开放；云端部署恒 403。
身份来自桥接头解析出的当前用户（与云端同一账号），视图按该用户重建。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/desktop/capabilities", tags=["Desktop Capabilities"])


def _require_desktop_store() -> None:
    from core.auth.desktop_bridge import bridge_enabled
    from core.capabilities.paths import capabilities_enabled

    if not bridge_enabled():
        raise HTTPException(status_code=403, detail="仅桌面双端本机后端可用")
    if not capabilities_enabled():
        raise HTTPException(status_code=403, detail="本机未配置能力文件存储（HUGAGENT_CAPS_ROOT）")


def _authorized_profile(user_id: str) -> Optional[str]:
    from core.capabilities import skills

    return skills.current_account_profile() if skills.account_authorized_for(user_id) else None


def _bridge_state(user_id: str) -> Dict[str, Any]:
    from core.services.desktop_cloud_bridge import get_state

    st = get_state()
    if not st:
        raise HTTPException(
            status_code=409, detail={"code": "cloud_unavailable", "message": "尚未登录云端账号"}
        )
    if not _authorized_profile(user_id):
        raise HTTPException(status_code=403, detail="cloud account does not match current user")
    return st


def _device_item(inst, *, runtime_name: str, usable: bool) -> Dict[str, Any]:
    item = inst.to_dict()
    item.update(
        registered=True,
        profile=inst.profile_id,
        revision=inst.resolved_revision,
        runtime_name=runtime_name,
        usable=bool(usable and inst.enabled),
    )
    return item


def _readiness_context(user_id: str):
    from core.capabilities.readiness import context_for_user

    connectors = _connectors_view(user_id)
    available = {
        entry["server_id"]
        for entry in connectors["items"]
        if entry["usable"] and entry["resolution"]["outcome"] == "chosen"
    }
    return context_for_user(user_id, available_mcp=available)


def _connectors_view(user_id: str) -> Dict[str, Any]:
    """Connector bindings as the resolver last decided them for this device."""
    from core.capabilities import connectors, skills, mcp_json
    from core.services.desktop_cloud_bridge import keep_local_bases
    from core.services.mcp_service import McpServerConfigService

    svc = McpServerConfigService.get_instance()
    from core.services.desktop_capability import component_base_name

    all_cfgs = {
        sid: cfg
        for sid, cfg in svc.get_all_servers(enabled_only=False).items()
        if cfg.get("owner_user_id") in (None, user_id)
    }
    all_cfgs.update(svc.get_owned_servers(user_id, enabled_only=False))
    enabled_ids = set(svc.get_all_servers(enabled_only=True)) | set(svc.get_owned_servers(user_id))
    base_map = {
        sid: component_base_name(sid, cfg.get("source_plugin"), cfg.get("owner_user_id"))
        for sid, cfg in all_cfgs.items()
    }
    from core.services.desktop_cloud_bridge import _mcp_json_local_declarations

    candidates = connectors.db_candidates(base_map, enabled_ids) + connectors.json_candidates(
        _mcp_json_local_declarations()
    )
    # The runtime context excludes disabled bindings; the management view must
    # keep them visible so users can turn them back on.
    from core.services.desktop_cloud_bridge import get_cached_manifest

    profile = _authorized_profile(user_id)
    manifest = get_cached_manifest() if profile else None
    if manifest:
        candidates += connectors.cloud_candidates(
            profile, manifest.get("servers") or [], mcp_json.managed_enabled(profile)
        )
    res = connectors.resolve_bindings(candidates, keep_local=keep_local_bases(), user_id=user_id)
    chosen = {c.install_id for c in res.chosen.values()}
    shadowed = {c.install_id for cs in res.shadowed.values() for c in cs}
    conflicted = {c.install_id for cs in res.conflicts.values() for c in cs}
    items = []
    for c in candidates:
        entry = c.to_dict()
        entry["server_id"] = connectors.server_id_of(c)
        entry["enabled"] = c.state != "disabled"
        entry["transport"] = (
            "cloud_gateway"
            if c.source == "cloud"
            else (
                _mcp_json_local_declarations().get(entry["server_id"], {}).get("transport")
                if c.profile == connectors.MCP_JSON_PROFILE
                else all_cfgs.get(entry["server_id"], {}).get("transport")
            )
        )
        outcome = (
            "chosen"
            if c.install_id in chosen
            else (
                "conflict"
                if c.install_id in conflicted
                else "shadowed" if c.install_id in shadowed else "unusable"
            )
        )
        entry["resolution"] = {"outcome": outcome, "reason": res.reasons.get(c.runtime_name)}
        from core.capabilities.readiness import apply_to_item, connector_readiness

        config = (
            _mcp_json_local_declarations().get(entry["server_id"], {})
            if c.profile == connectors.MCP_JSON_PROFILE
            else all_cfgs.get(entry["server_id"], {})
        )
        apply_to_item(entry, connector_readiness(c, config))
        items.append(entry)
    return {
        "kind": "mcp",
        "profile_id": _authorized_profile(user_id),
        "items": items,
        "conflicts": {n: [c.install_id for c in cs] for n, cs in res.conflicts.items()},
        "preferences": _preferences("mcp", user_id),
    }


def _agents_view(user_id: str) -> Dict[str, Any]:
    from core.capabilities import agents as caps_agents
    from core.capabilities import registry, skills
    from core.capabilities.paths import KIND_AGENT
    from core.db.engine import SessionLocal
    from core.services.user_agent_service import UserAgentService

    with SessionLocal() as db:
        svc = UserAgentService(db)
        local_rows = [svc._serialize(a) for a in svc.repo.list_for_user(user_id)]
    res = caps_agents.resolve_visible(user_id, local_rows)
    chosen = {c.install_id for c in res.chosen.values()}
    shadowed = {c.install_id for cs in res.shadowed.values() for c in cs}
    conflicted = {c.install_id for cs in res.conflicts.values() for c in cs}
    items: List[Dict[str, Any]] = []
    profile = _authorized_profile(user_id)
    context = _readiness_context(user_id)
    from core.capabilities.readiness import apply_to_item, file_readiness, files_ready

    for inst in registry.list_installations(
        kind=KIND_AGENT, profiles=[p for p in ("local", profile) if p]
    ):
        if inst.profile_id == "local" and inst.payload.get("owner_user_id") not in (None, user_id):
            continue
        entry = _device_item(inst, runtime_name=inst.display_name, usable=inst.ready)
        outcome = (
            "chosen"
            if inst.install_id in chosen
            else (
                "conflict"
                if inst.install_id in conflicted
                else "shadowed" if inst.install_id in shadowed else "unusable"
            )
        )
        entry["resolution"] = {"outcome": outcome, "reason": res.reasons.get(inst.display_name)}
        apply_to_item(entry, file_readiness(inst, context), downloaded=files_ready(inst))
        items.append(entry)
    return {
        "kind": "agent",
        "profile_id": profile,
        "items": items,
        "conflicts": {n: [c.install_id for c in cs] for n, cs in res.conflicts.items()},
        "preferences": _preferences("agent", user_id),
    }


def _plugins_view(user_id: str) -> Dict[str, Any]:
    from core.capabilities import registry
    from core.capabilities.paths import KIND_PLUGIN

    profile = _authorized_profile(user_id)
    context = _readiness_context(user_id)
    from core.capabilities.readiness import file_readiness, files_ready

    items: List[Dict[str, Any]] = []
    for inst in registry.list_installations(
        kind=KIND_PLUGIN, profiles=[p for p in ("local", profile) if p]
    ):
        owner = inst.payload.get("owner_user_id")
        if owner and owner != user_id:
            continue
        readiness = file_readiness(inst, context)
        entry = _device_item(inst, runtime_name=inst.key, usable=readiness["ready"])
        entry["readiness"] = readiness
        entry["files_ready"] = files_ready(inst)
        entry["resolution"] = {
            "outcome": "chosen" if entry["readiness"]["ready"] else "unusable",
            "reason": None,
        }
        items.append(entry)
    return {
        "kind": "plugin",
        "profile_id": profile,
        "items": items,
        "conflicts": {},
        "preferences": {},
    }


def _installations_view(user_id: str, kind: str) -> Dict[str, Any]:
    from core.capabilities import skills
    from core.capabilities.paths import KIND_AGENT, KIND_MCP, KIND_PLUGIN, KIND_SKILL

    if kind == KIND_MCP:
        return _connectors_view(user_id)
    if kind == KIND_AGENT:
        return _agents_view(user_id)
    if kind == KIND_PLUGIN:
        return _plugins_view(user_id)
    if kind != KIND_SKILL:
        raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    res = skills.resolve_for_user(user_id)
    chosen = {c.install_id: name for name, c in res.chosen.items()}
    shadowed = {c.install_id: name for name, cs in res.shadowed.items() for c in cs}
    conflicted = {c.install_id: name for name, cs in res.conflicts.items() for c in cs}
    unusable = {c.install_id: name for name, cs in res.unusable.items() for c in cs}

    def _resolution_of(install_id: str) -> Dict[str, Any]:
        if install_id in chosen:
            return {"outcome": "chosen", "reason": res.reasons.get(chosen[install_id])}
        if install_id in conflicted:
            return {"outcome": "conflict", "reason": res.reasons.get(conflicted[install_id])}
        if install_id in shadowed:
            return {"outcome": "shadowed", "reason": res.reasons.get(shadowed[install_id])}
        if install_id in unusable:
            return {"outcome": "unusable", "reason": res.reasons.get(unusable[install_id])}
        return {"outcome": "absent", "reason": None}

    context = _readiness_context(user_id)
    from core.capabilities.readiness import (
        apply_to_item,
        builtin_readiness,
        file_readiness,
        files_ready,
    )

    items: List[Dict[str, Any]] = []
    for cand in skills.candidates(user_id):
        entry = cand.to_dict()
        from core.capabilities import registry

        inst = registry.get(cand.install_id)
        entry["registered"] = inst is not None
        entry["enabled"] = inst.enabled if inst else True
        if inst:
            for key in ("derived_from", "derived_resource_ref", "derived_revision"):
                if key in inst.payload:
                    entry[key] = inst.payload[key]
        entry["resolution"] = _resolution_of(cand.install_id)
        report = file_readiness(inst, context) if inst else builtin_readiness(cand, context)
        apply_to_item(entry, report, downloaded=files_ready(inst) if inst else cand.path is not None)
        items.append(entry)
    return {
        "kind": kind,
        "profile_id": _authorized_profile(user_id),
        "items": items,
        "conflicts": {name: [c.install_id for c in cs] for name, cs in res.conflicts.items()},
        "preferences": _preferences(kind, user_id),
    }


def _preferences(kind: str, user_id: str) -> Dict[str, str]:
    from core.capabilities import registry

    return registry.preferences(kind, user_id=user_id)


@router.get("/installations", summary="本机能力：意图、设备状态与解析结果")
async def list_installations(kind: str = "skill", user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    return success_response(
        data=await asyncio.to_thread(_installations_view, str(user.user_id), kind)
    )


@router.post("/sync", summary="立即同步云端能力清单")
async def sync_now(user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    st = _bridge_state(str(user.user_id))
    from core.services import desktop_cloud_bundles, desktop_cloud_skills
    from core.services.desktop_cloud_bridge import sync_capabilities_blocking

    await asyncio.to_thread(sync_capabilities_blocking, st)
    status = desktop_cloud_skills.status()
    status["bundles"] = desktop_cloud_bundles.status()
    errors = [status.get("last_error")] + [v.get("last_error") for v in status["bundles"].values()]
    errors = [e for e in errors if e]
    if errors:
        raise HTTPException(
            status_code=502,
            detail={"code": "cloud_unavailable", "message": "; ".join(errors), "retryable": True},
        )
    from core.capabilities import skills

    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    return success_response(data=status)


@router.post("/views/rebuild", summary="重建运行视图")
async def rebuild_views(user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    from core.capabilities import skills

    reports = await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    return success_response(data={k: v.to_dict() for k, v in reports.items()})


# ── mcp.json ──────────────────────────────────────────────────────────


def _mcp_json_doc(user_id: str) -> Dict[str, Any]:
    from core.capabilities import mcp_json, skills

    try:
        doc = mcp_json.load()
    except mcp_json.McpJsonCorrupt as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "integrity_failed",
                "message": str(exc),
                "recovery_action": "repair_mcp_json",
            },
        ) from exc
    return {
        "path": str(mcp_json.mcp_json_path()),
        "generation": doc.generation,
        "digest": doc.digest,
        "local": doc.local,
        "managedProfiles": {
            p: v for p, v in doc.managed.items() if p == _authorized_profile(user_id)
        },
    }


@router.get("/mcp-json", summary="mcp.json 投影（不含任何密钥）")
async def get_mcp_json(_user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    return success_response(data=await asyncio.to_thread(_mcp_json_doc, str(_user.user_id)))
