"""本机能力安装接口（桌面双端的本机后端侧）。

  GET  /v1/desktop/capabilities/installations      账号意图 + 设备状态 + 本轮解析结果
  POST /v1/desktop/capabilities/sync               立即拉取云端清单（用户主动刷新）
  POST /v1/desktop/capabilities/preparations       在本机准备（下载、校验、发布、联接）
  POST /v1/desktop/capabilities/removals           移除本设备文件（账号意图保留）
  PUT  /v1/desktop/capabilities/name-preferences   同名冲突的显式选择（仅本设备）
  POST /v1/desktop/capabilities/views/rebuild      重建运行视图

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
from pydantic import BaseModel, Field

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


def _visible_installation(install_id: str, user_id: str):
    from core.capabilities import registry, skills
    from core.capabilities.paths import LOCAL_PROFILE

    inst = registry.get(install_id)
    if inst is not None:
        if inst.profile_id == LOCAL_PROFILE:
            owner = inst.payload.get("owner_user_id")
            if not owner or owner == user_id:
                return inst
        elif inst.profile_id == _authorized_profile(user_id):
            return inst
    raise HTTPException(status_code=404, detail="installation not found")


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


class PreparationBody(BaseModel):
    install_ids: List[str] = Field(default_factory=list, max_length=200)
    resource_refs: List[Dict[str, str]] = Field(default_factory=list, max_length=200)
    sync_first: bool = Field(
        default=False, description="先拉一次云端清单再准备（安装后立即调用时用）"
    )


@router.post("/preparations", summary="在本机准备能力（下载、校验、发布、联接）")
async def prepare(body: PreparationBody, user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    st = None
    from core.capabilities import registry, skills
    from core.capabilities.ref import ResourceRef
    from core.services import desktop_cloud_skills

    if body.sync_first:
        from core.services.desktop_cloud_bridge import sync_capabilities_blocking

        st = _bridge_state(str(user.user_id))
        await asyncio.to_thread(sync_capabilities_blocking, st)
    profile = _authorized_profile(str(user.user_id))
    ids = list(body.install_ids)
    for raw in body.resource_refs:
        try:
            ref = ResourceRef.from_dict(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid resource_ref") from exc
        iid = registry.install_id(ref.kind, profile or "", ref.key)
        inst = _visible_installation(iid, str(user.user_id))
        if inst.ref != ref:
            raise HTTPException(status_code=404, detail="installation not found")
        ids.append(iid)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise HTTPException(status_code=400, detail="install_ids or resource_refs required")
    installations = [_visible_installation(iid, str(user.user_id)) for iid in ids]
    from core.services import desktop_cloud_bundles

    results = []
    for inst in installations:
        if inst.profile_id == "local":
            results.append(
                {"install_id": inst.install_id, "ok": inst.ready, "installation": inst.to_dict()}
            )
        elif inst.kind == "skill":
            st = st or _bridge_state(str(user.user_id))
            results.extend(
                await asyncio.to_thread(desktop_cloud_skills.prepare, st, [inst.install_id])
            )
        elif inst.kind in ("agent", "plugin"):
            st = st or _bridge_state(str(user.user_id))
            results.extend(
                await asyncio.to_thread(desktop_cloud_bundles.prepare, st, [inst.install_id])
            )
        else:
            raise HTTPException(
                status_code=400, detail="connector readiness is managed through mcp.json"
            )
    # Check the closure after every requested package has been published. A
    # plugin listed first must see a skill prepared later in this same request.
    context = await asyncio.to_thread(_readiness_context, str(user.user_id))
    from core.capabilities.readiness import file_readiness, files_ready

    for result in results:
        inst = registry.get(result["install_id"])
        if inst is None:
            continue
        readiness = await asyncio.to_thread(file_readiness, inst, context)
        result["files_ready"] = files_ready(inst)
        result["readiness"] = readiness
        if result.get("ok") and not readiness["ready"]:
            result["ok"] = False
            result["error"] = {
                "code": "dependency_missing",
                "message": "文件已保留，平台或必要依赖尚未就绪",
                "recovery_action": "inspect_dependencies",
                "details": {"dependencies": readiness["errors"]},
            }
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    return success_response(data={"results": results})


class LocalCopyBody(BaseModel):
    runtime_name: Optional[str] = Field(default=None, min_length=1, max_length=160)


@router.post("/installations/{install_id}/local-copy", summary="创建独立的本机技能副本")
async def create_local_copy(
    install_id: str, body: LocalCopyBody, user: UserContext = Depends(get_current_user)
):
    _require_desktop_store()
    inst = _visible_installation(install_id, str(user.user_id))
    st = _bridge_state(str(user.user_id))
    from core.capabilities import skills
    from core.capabilities.errors import CapabilityError
    from core.capabilities.local_copy import create_skill_copy

    try:
        copied = await asyncio.to_thread(
            create_skill_copy,
            inst,
            user_id=str(user.user_id),
            state=st,
            runtime_name=body.runtime_name,
        )
    except CapabilityError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    _invalidate_capability_caches()
    listing = await asyncio.to_thread(_installations_view, str(user.user_id), "skill")
    item = next(i for i in listing["items"] if i["install_id"] == copied.install_id)
    return success_response(data={"install_id": copied.install_id, "installation": item})


class SkillFileBody(BaseModel):
    content: str = Field(..., max_length=1024 * 1024)
    expected_revision: str = Field(..., min_length=1, max_length=64)


@router.get("/installations/{install_id}/files/{filename:path}", summary="读取本机技能副本文件")
async def read_local_skill_file(
    install_id: str, filename: str, user: UserContext = Depends(get_current_user)
):
    _require_desktop_store()
    inst = _visible_installation(install_id, str(user.user_id))
    from core.capabilities.local_copy import read_file
    from core.capabilities.errors import CapabilityError

    try:
        result = await asyncio.to_thread(
            read_file, inst, user_id=str(user.user_id), filename=filename
        )
    except CapabilityError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return success_response(data=result)


@router.put("/installations/{install_id}/files/{filename:path}", summary="保存本机技能副本的新版本")
async def save_local_skill_file(
    install_id: str,
    filename: str,
    body: SkillFileBody,
    user: UserContext = Depends(get_current_user),
):
    _require_desktop_store()
    inst = _visible_installation(install_id, str(user.user_id))
    from core.capabilities.local_copy import update_file
    from core.capabilities import skills
    from core.capabilities.errors import CapabilityError

    try:
        updated = await asyncio.to_thread(
            update_file,
            inst,
            user_id=str(user.user_id),
            filename=filename,
            content=body.content,
            expected_revision=body.expected_revision,
        )
    except CapabilityError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    _invalidate_capability_caches()
    listing = await asyncio.to_thread(_installations_view, str(user.user_id), "skill")
    item = next(i for i in listing["items"] if i["install_id"] == updated.install_id)
    return success_response(
        data={
            "filename": filename,
            "content": body.content,
            "revision": updated.resolved_revision,
            "installation": item,
            "is_binary": False,
        }
    )


class DeviceEnabledBody(BaseModel):
    enabled: bool


@router.put("/installations/{install_id}/enabled", summary="启用或停用本机能力")
async def set_installation_enabled(
    install_id: str, body: DeviceEnabledBody, user: UserContext = Depends(get_current_user)
):
    _require_desktop_store()
    from core.capabilities import registry, skills

    inst = _visible_installation(install_id, str(user.user_id))
    registry.set_state(
        inst.install_id, inst.state, payload_update={"device_enabled_override": body.enabled}
    )
    registry.set_enabled(inst.install_id, body.enabled and inst.payload.get("source_enabled", True))
    skills.bump_view_generation()
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    _invalidate_capability_caches()
    return success_response(
        data=await asyncio.to_thread(_installations_view, str(user.user_id), inst.kind)
    )


class RemovalBody(BaseModel):
    install_id: str = Field(..., min_length=1)
    target: str = Field(..., pattern="^(device|account)$")


@router.post("/removals", summary="移除本设备文件（账号意图保留）")
async def remove(body: RemovalBody, user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    if body.target == "account":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "account_removal_is_cloud_side",
                "message": "从账号移除要在云端能力中心操作，本机只能移除此设备的文件",
            },
        )
    from core.capabilities import registry, skills, store
    from core.capabilities.paths import LOCAL_PROFILE

    inst = _visible_installation(body.install_id, str(user.user_id))
    from core.capabilities.runtime import references

    retained = await asyncio.to_thread(references, inst.kind, inst.profile_id, inst.key)
    if retained:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_in_use",
                "message": "该版本被运行记录引用，保留文件以便恢复运行",
                "run_count": len(retained),
            },
        )
    if inst.profile_id == LOCAL_PROFILE:
        from core.capabilities import agents, plugins

        removers = {
            "skill": skills.remove_local_skill,
            "agent": agents.remove_local_agent,
            "plugin": plugins.remove_local_plugin,
        }
        if inst.kind not in removers:
            raise HTTPException(status_code=400, detail="use mcp.json to remove a connector")
        await asyncio.to_thread(removers[inst.kind], inst.key)
    else:
        store.remove_key(inst.kind, inst.profile_id, inst.key)
        registry.set_state(inst.install_id, "pending", resolved_revision=None)
    skills.bump_view_generation()
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    _invalidate_capability_caches()
    return success_response(
        data={
            "install_id": body.install_id,
            "state": "removed" if inst.profile_id == LOCAL_PROFILE else "pending",
        }
    )


class NamePreferenceBody(BaseModel):
    kind: str = Field(default="skill")
    runtime_name: str = Field(..., min_length=1, max_length=160)
    install_id: Optional[str] = Field(default=None, description="null = 清除偏好")


@router.put("/name-preferences", summary="同名冲突的显式选择（仅本设备）")
async def set_name_preference(
    body: NamePreferenceBody, user: UserContext = Depends(get_current_user)
):
    _require_desktop_store()
    from core.capabilities import registry, skills

    if body.kind not in ("skill", "mcp", "agent"):
        raise HTTPException(status_code=400, detail=f"kind {body.kind!r} 尚未接入本机存储")
    if body.install_id:
        listing = await asyncio.to_thread(_installations_view, str(user.user_id), body.kind)
        cands = [c for c in listing["items"] if c["install_id"] == body.install_id]
        if not cands or cands[0]["runtime_name"] != body.runtime_name:
            raise HTTPException(status_code=404, detail="该候选不存在或不属于这个名字")
        registry.set_preference(
            body.kind, body.runtime_name, body.install_id, chosen_by=str(user.user_id)
        )
    else:
        registry.clear_preference(body.kind, body.runtime_name, user_id=str(user.user_id))
    skills.bump_view_generation()
    await asyncio.to_thread(skills.rebuild_views, str(user.user_id))
    _invalidate_capability_caches()
    return success_response(
        data=await asyncio.to_thread(_installations_view, str(user.user_id), body.kind)
    )


def _invalidate_capability_caches() -> None:
    from core.agent_skills.cache_refresh import refresh_skill_caches

    refresh_skill_caches()
    from core.services.mcp_service import McpServerConfigService

    McpServerConfigService.get_instance().invalidate_cache()


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


class LocalMcpServerBody(BaseModel):
    expected_generation: Optional[int] = Field(default=None, ge=0)
    expected_digest: Optional[str] = None
    transport: str = Field(..., pattern="^(stdio|streamable_http|sse)$")
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    cwd: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None
    displayName: Optional[str] = None
    description: Optional[str] = None
    executionTimeout: Optional[int] = Field(default=None, ge=1, le=3600)
    enabled: bool = True
    secret_headers: Dict[str, str] = Field(
        default_factory=dict, description="进设备凭据库，不落文件"
    )


@router.put("/mcp-json/local/{server_id}", summary="新增/替换本机 MCP 声明")
async def put_local_mcp(
    server_id: str, body: LocalMcpServerBody, user: UserContext = Depends(get_current_user)
):
    _require_desktop_store()
    from core.capabilities import credentials, mcp_json

    spec = body.model_dump(
        exclude={"secret_headers", "expected_generation", "expected_digest"}, exclude_none=True
    )
    try:
        await asyncio.to_thread(
            mcp_json.upsert_local_server,
            server_id,
            spec,
            secret_headers=body.secret_headers or None,
            expected_generation=body.expected_generation,
            expected_digest=body.expected_digest,
        )
    except credentials.CredentialStoreUnavailable as exc:
        raise HTTPException(
            status_code=503, detail={"code": "credential_store_unavailable", "message": str(exc)}
        )
    except mcp_json.McpJsonError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "install_conflict", "message": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _invalidate_capability_caches()
    return success_response(data=await asyncio.to_thread(_mcp_json_doc, str(user.user_id)))


@router.delete("/mcp-json/local/{server_id}", summary="删除本机 MCP 声明")
async def delete_local_mcp(
    server_id: str,
    expected_generation: Optional[int] = None,
    expected_digest: Optional[str] = None,
    _user: UserContext = Depends(get_current_user),
):
    _require_desktop_store()
    from core.capabilities import mcp_json

    try:
        removed = await asyncio.to_thread(
            mcp_json.remove_local_server,
            server_id,
            expected_generation=expected_generation,
            expected_digest=expected_digest,
        )
    except mcp_json.McpJsonError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="local server not found")
    _invalidate_capability_caches()
    return success_response(data=await asyncio.to_thread(_mcp_json_doc, str(_user.user_id)))


class ManagedEnabledBody(BaseModel):
    enabled: bool


@router.put(
    "/mcp-json/managed/{profile}/{server_id}/enabled",
    summary="停用/启用一个云端连接器（仅本设备偏好）",
)
async def set_managed_enabled(
    profile: str,
    server_id: str,
    body: ManagedEnabledBody,
    _user: UserContext = Depends(get_current_user),
):
    _require_desktop_store()
    from core.capabilities import mcp_json, skills

    if profile != _authorized_profile(str(_user.user_id)):
        raise HTTPException(status_code=404, detail="managed profile not found")
    try:
        await asyncio.to_thread(mcp_json.set_managed_enabled, profile, server_id, body.enabled)
    except mcp_json.McpJsonError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _invalidate_capability_caches()
    return success_response(data=await asyncio.to_thread(_mcp_json_doc, str(_user.user_id)))


@router.post("/mcp-json/repair", summary="把无法解析的 mcp.json 移到一旁（显式修复动作）")
async def repair_mcp_json(_user: UserContext = Depends(get_current_user)):
    _require_desktop_store()
    from core.capabilities import mcp_json

    moved = await asyncio.to_thread(mcp_json.quarantine_corrupt)
    return success_response(data={"quarantined": str(moved) if moved else None})
