"""Agent assembly phase: server selection. """

from __future__ import annotations

from core.capabilities.paths import capabilities_enabled
from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.tools import mcp_config as factory_mcp_config
from core.services.mcp_service import McpServerConfigService


async def server_selection(
    request: AgentRequest,
    *,
    _api_scope,
    _elapsed,
    _eval_scope,
    _log,
    _note_unavailable,
    required: RequiredCapabilities,
    sticky: StickyCapabilities,
    cfg,
    skill_bound_mcp_ids,
):
    asset_bundle = None

    # The current user's self-added private MCPs (owner-isolated, queried from the DB on demand)
    owned_mcp_servers: dict = {}
    if request.current_user_id and _eval_scope is None:
        try:
            owned_mcp_servers = McpServerConfigService.get_instance().get_owned_servers(
                str(request.current_user_id),
                # A sub-agent's binding is explicit and may opt into one of the
                # owner's personally disabled MCPs without enabling it for the
                # main agent. The explicit enabled_mcp_ids list below remains
                # the final allowlist, so unrelated private MCPs are not loaded.
                enabled_only=not (
                    request.user_agent is not None
                    or skill_bound_mcp_ids
                    or required.connector_ids
                    or required.plugin_mcp_ids
                    or sticky.direct_mcp_ids
                    or sticky.plugin_mcp_ids
                ),
            )
        except Exception:
            owned_mcp_servers = {}

    # 双端桌面本机后端：云端授权 MCP 以「指向云端能力网关的 HTTP MCP」形态
    # 作为独立配置源（bridge_servers）进入装配，最终仍受 enabled_mcp_ids
    # allowlist 收口。云端部署 / 纯本机模式下桥未激活，此处为空 dict。
    bridge_mcp_servers: dict = {}
    _capability_mcp_resolution = []
    _unavailable_connectors = {}
    try:
        from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs

        bridge_mcp_servers = cloud_gateway_mcp_configs(
            request.enabled_mcp_ids,
            resolution_out=_capability_mcp_resolution,
            unavailable_out=_unavailable_connectors,
        )
    except Exception:  # noqa: BLE001
        if capabilities_enabled():
            raise  # a desktop binding failure must not fall back to another source
        bridge_mcp_servers = {}

    if _api_scope is not None and bridge_mcp_servers:
        raise ValueError("API 模式不能借用个人桌面能力网关，请使用已发布的服务端 MCP")

    for _sid, _reason in _unavailable_connectors.items():
        _note_unavailable(f"连接器「{_sid}」暂不可用（{_reason}）。")
    if request.enabled_mcp_ids is not None:
        request.enabled_mcp_ids = [
            sid for sid in request.enabled_mcp_ids if sid not in _unavailable_connectors
        ]

    # Determine which MCP servers to connect
    enabled_mcp_keys = factory_mcp_config._effective_mcp_server_keys(
        cfg,
        request.agent_spec,
        enabled_mcp_ids=request.enabled_mcp_ids,
        enabled_kb_ids=request.enabled_kb_ids,
        owned_servers=owned_mcp_servers,
        bridge_servers=bridge_mcp_servers,
    )

    _log.info("[factory] +%s capability selection resolved", _elapsed())

    return (
        _capability_mcp_resolution,
        asset_bundle,
        bridge_mcp_servers,
        enabled_mcp_keys,
        owned_mcp_servers,
    )
