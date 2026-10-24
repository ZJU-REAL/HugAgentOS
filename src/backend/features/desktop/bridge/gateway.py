"""gateway responsibilities."""
from __future__ import annotations
from features.desktop.bridge import client as bridge_client
from typing import Any, Dict, List, Optional, Set
from features.desktop.auth import bridge as bridge_auth
import logging
logger = logging.getLogger(__name__)
from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.bridge import managed_connectors as bridge_managed_connectors

def bridge_context() -> Optional[Dict[str, Any]]:
    """唯一守门点：桥激活且 manifest 就绪时返回上下文，否则 None。

    返回 {"state": st, "servers": [含 component 的云端 server，已剔除 mcp.json 里
    用户停用的项], "profile": 账号 profile}。同名裁决交给解析器。
    server 列表按 (账号, 清单 revision, 停用集合) 记忆化；``state`` 每次现取，
    因为它携带轮换中的 token。
    """
    pass  # State is owned by its responsibility module.
    if not bridge_auth.bridge_enabled():
        return None
    st = bridge_identity.get_state()
    if not st:
        return None
    manifest = bridge_client.get_cached_manifest()
    if not manifest:
        return None
    enabled = bridge_managed_connectors.managed_enabled(st)
    key = (bridge_identity.state_fingerprint(st), str(manifest["revision"]), tuple(sorted(enabled.items())))
    with bridge_client_state.context_lock:
        hit = bridge_client_state.context_cache
    if hit is None or hit[0] != key:
        servers: List[Dict[str, Any]] = []
        for s in manifest.get("servers") or []:
            if not isinstance(s, dict):
                continue
            # A server with no current tool contracts cannot replace a local
            # implementation. The manifest validator already guarantees every
            # non-empty entry contains complete, cloud-supplied schemas.
            if not s.get("tools"):
                continue
            sid = str(s.get("server_id") or "").strip()
            if not sid or not enabled.get(sid, True):
                continue
            entry = dict(s)
            entry["server_id"] = sid
            entry["component"] = str(s.get("component") or sid).strip()
            servers.append(entry)
        hit = (
            key,
            {
                "servers": servers,
                "manifest_revision": str(manifest["revision"]),
                "profile": bridge_managed_connectors.account_profile(st),
            },
        )
        with bridge_client_state.context_lock:
            bridge_client_state.context_cache = hit
    if not hit[1]["servers"]:
        return None
    return {"state": st, **hit[1]}


def mcp_json_local_configs() -> Dict[str, dict]:
    from features.desktop.capabilities import mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.local_server_configs()
    except mcp_json.McpJsonError as exc:
        logger.warning("[cloud-bridge] mcp.json local servers unavailable: %s", exc)
        return {}


def cloud_gateway_mcp_configs(
    enabled_mcp_ids: Optional[List[str]] = None, *, resolution_out=None, unavailable_out=None
) -> Dict[str, dict]:
    """Desktop-only MCP config sources: cloud gateway bindings + mcp.json local servers.

    Only the components the resolver chose for the cloud binding get a gateway
    config; the run's enabled-id allowlist still collapses the final set.
    """
    from features.desktop.capabilities import connectors

    configs: Dict[str, dict] = dict(mcp_json_local_configs())
    ctx = bridge_context()
    res = resolve_mcp_bindings(enabled_mcp_ids, ctx)
    if resolution_out is not None:
        resolution_out.append(res)
    requested = set(enabled_mcp_ids or [])
    for name, group in {**res.conflicts, **res.unusable}.items():
        if name in requested or any(connectors.server_id_of(c) in requested for c in group):
            from core.harness.capability_errors import NameConflict, PackageMissing

            error = NameConflict if name in res.conflicts else PackageMissing
            if unavailable_out is not None:
                unavailable_out.update({connectors.server_id_of(c): error.code for c in group})
                continue
            raise error(
                "selected connector binding is unavailable; choose its source", runtime_name=name
            )
    chosen = res.chosen_ids()
    configs = {
        sid: cfg
        for sid, cfg in configs.items()
        if f"mcp:{connectors.MCP_JSON_PROFILE}:{sid}" in chosen
    }
    if not ctx:
        return configs
    st = ctx["state"]
    manifest_revision = ctx["manifest_revision"]
    for s in ctx["servers"]:
        # Source selection is complete; only the exact authorized cloud binding
        # chosen by the resolver may produce a gateway config.
        sid = s["server_id"]
        if f"mcp:{ctx['profile']}:{sid}" not in chosen:
            continue
        from features.desktop.bridge.resource_plugins import local_config
        from features.desktop.capabilities.skill_account import current_local_user_id
        native = local_config(s, current_local_user_id(), ctx["profile"])
        if native is not None:
            configs[sid] = native
            continue
        invoke_url = f"{st['cloud_base']}/api/v1/desktop/capability/gateway/{sid}/call"
        configs[sid] = {
            "transport": "streamable_http",
            # This endpoint invokes the authorized manifest tool through JSON.
            "url": invoke_url,
            "headers": bridge_identity.cloud_headers(st),
            "execution_timeout": 180,
            "schema_source": "cloud_manifest",
            "manifest_revision": manifest_revision,
            "manifest_tools": s["tools"],
            "schema_hash": str(s["schema_hash"]),
            "gateway_invoke_url": invoke_url,
            "gateway_plugin": str(s.get("source_plugin") or ""),
        }
    return configs


def local_server_ids() -> Set[str]:
    """本机 DB 内全部 MCP 的 server_id。

    复用 ``McpServerConfigService`` 自带的 30s 缓存与失效链路，不另建缓存。
    """
    try:
        from core.capabilities.mcp.service import McpServerConfigService

        return set(McpServerConfigService.get_instance().get_all_servers(enabled_only=False))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cloud-bridge] local server ids failed: %s", exc)
        return set()


def apply_to_enabled_mcp_ids(mcp_ids: Optional[List[str]]) -> Optional[List[str]]:
    """把本轮 enabled_mcp_ids 交给解析器，按 server_id 裁决每个连接器的绑定。

    候选：本机目录行（device）、云端 manifest（account）、mcp.json 本机声明。
    同一 server_id 多个候选时，云端账号级候选默认胜出，
    用户可用同名偏好显式改选；落选绑定记为 shadowed，可在界面
    切换，而不是被静默顶掉。云端 manifest 尚未就绪 / 桥未激活：原样返回。
    顺序稳定（已选项按原顺序，新增云端与 mcp.json 项追加），幂等。
    """
    if mcp_ids is None:
        return None
    ctx = bridge_context()
    from features.desktop.capabilities import connectors

    local_ids = local_server_ids()
    json_local = mcp_json_local_declarations()
    res = resolve_mcp_bindings(mcp_ids, ctx)

    chosen_ids = {connectors.server_id_of(c) for c in res.chosen.values()}
    known = local_ids | {s["server_id"] for s in (ctx or {}).get("servers", [])} | set(json_local)
    kept = [mid for mid in mcp_ids if mid in chosen_ids or mid not in known]
    existing = set(kept)
    for name in sorted(res.chosen):
        sid = connectors.server_id_of(res.chosen[name])
        if sid not in existing:
            kept.append(sid)
            existing.add(sid)
    return kept


def resolve_mcp_bindings(mcp_ids: Optional[List[str]], ctx):
    from features.desktop.capabilities import connectors

    local_ids = local_server_ids()
    enabled = set(local_ids) if mcp_ids is None else set(mcp_ids)
    candidates = connectors.db_candidates(local_ids, enabled) + connectors.json_candidates(
        mcp_json_local_declarations()
    )
    if ctx:
        candidates += connectors.cloud_candidates(ctx["profile"], ctx["servers"], {})
    return connectors.resolve_bindings(candidates)


def mcp_json_local_declarations() -> Dict[str, Dict[str, Any]]:
    from features.desktop.capabilities import mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.load().local
    except mcp_json.McpJsonError:
        return {}


def apply_to_enabled_skill_ids(skill_ids: Optional[List[str]]) -> Optional[List[str]]:
    """把云端技能合并进本轮 enabled_skill_ids（云端为真源，见 desktop_cloud_skills）。"""
    if skill_ids is None or not bridge_identity.bridge_active():
        return skill_ids
    from features.desktop.bridge import skills as desktop_cloud_skills

    return desktop_cloud_skills.apply_to_enabled_skill_ids(skill_ids)


def local_plugin_configs(user_id, enabled_only=True):
    from features.desktop.capabilities.local_plugin_runtime import configs
    return configs(user_id, enabled_only=enabled_only)
