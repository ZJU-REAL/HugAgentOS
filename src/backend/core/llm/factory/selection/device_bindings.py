"""Agent assembly phase: device bindings. """

from __future__ import annotations

import asyncio

from core.capabilities import runtime as capability_runtime
from core.llm.agent_api_runtime import scope_mcp_servers
from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.tools import mcp_config as factory_mcp_config


async def device_bindings(
    request: AgentRequest,
    *,
    _api_scope,
    _capability_mcp_resolution,
    _caps_skill_ids,
    _desktop_progressive,
    _elapsed,
    _eval_scope,
    _log,
    _mode_plugin_ids,
    _note_unavailable,
    _prepared_capabilities,
    _progressive,
    required: RequiredCapabilities,
    sticky: StickyCapabilities,
    _subagent_progressive,
    bridge_mcp_servers,
    enabled_mcp_keys,
    owned_mcp_servers,
):
    _log.info("[factory] +%s capability snapshot prepared", _elapsed())

    _required_connector_server_keys = factory_mcp_config._required_mcp_server_keys(
        required.connector_ids,
        enabled_mcp_keys,
    )
    _required_plugin_server_keys = factory_mcp_config._required_mcp_server_keys(
        required.plugin_mcp_ids,
        enabled_mcp_keys,
    )
    if required.connector_ids and not _required_connector_server_keys:
        _note_unavailable("所选连接器当前不可用或未获授权：" + ", ".join(required.connector_ids))
    if (
        required.plugin_id
        and required.plugin_mcp_ids
        and not _required_plugin_server_keys
        and not required.plugin_skill_ids
    ):
        _note_unavailable(f"所选插件「{required.plugin_name}」的连接器当前不可用。")
    enabled_servers = factory_mcp_config._filter_mcp_servers_by_keys(
        enabled_mcp_keys,
        owned_servers=owned_mcp_servers,
        bridge_servers=bridge_mcp_servers,
    )
    if _prepared_capabilities is not None:
        enabled_servers = await asyncio.to_thread(
            capability_runtime.bind_mcp,
            _prepared_capabilities,
            enabled_servers,
            _capability_mcp_resolution[0] if _capability_mcp_resolution else None,
        )

    if _prepared_capabilities is not None:
        _dependency_plugins = [
            *list(getattr(request.user_agent, "plugin_ids", None) or []),
            *sticky.plugin_ids,
            *_mode_plugin_ids,
        ]
        if required.plugin_id:
            _dependency_plugins.append(required.plugin_id)
        _prepared_capabilities = await asyncio.to_thread(
            capability_runtime.preflight,
            _prepared_capabilities,
            plugin_ids=list(dict.fromkeys(_dependency_plugins)),
            available_mcp=set(enabled_servers),
            available_kb=set(request.enabled_kb_ids or []),
        )
        _log.info("[factory] +%s capability preflight completed", _elapsed())
        _unusable_skill_ids = {
            str(row.get("skill_id"))
            for row in _prepared_capabilities.dependency_report.get("unavailable_skills") or []
        }
        _unusable_skill_ids.update(
            set(_caps_skill_ids or []) - set(_prepared_capabilities.bindings)
        )
        for _name, _reason in _prepared_capabilities.unavailable.items():
            _note_unavailable(f"能力「{_name}」暂不可用（{_reason}）。")
        if _unusable_skill_ids:
            _log.info(
                "[factory] 本轮不提供依赖未满足的技能：%s",
                ", ".join(sorted(_unusable_skill_ids)),
            )
            _caps_skill_ids = [s for s in (_caps_skill_ids or []) if s not in _unusable_skill_ids]
            request.enabled_skill_ids = list(_caps_skill_ids)

    _desktop_prepared_servers = dict(enabled_servers)
    if _desktop_progressive is not None:
        request.enabled_skill_ids = [
            sid
            for sid in (_caps_skill_ids or [])
            if sid not in _desktop_progressive.deferred_skill_ids
        ]
        enabled_servers = {
            sid: config
            for sid, config in enabled_servers.items()
            if sid not in _desktop_progressive.deferred_mcp_ids
        }
        enabled_mcp_keys = list(enabled_servers)
        if request.user_agent is not None:
            _subagent_progressive = _desktop_progressive
        else:
            _progressive = _desktop_progressive

    enabled_servers = factory_mcp_config._inject_runtime_headers(
        enabled_servers,
        current_user_id=request.current_user_id,
        chat_id=request.chat_id,
        enabled_kb_ids=request.enabled_kb_ids,
        channel_origin=request.channel_origin,
        reranker_enabled=request.reranker_enabled,
    )

    if _api_scope is not None:
        enabled_servers = scope_mcp_servers(enabled_servers, _api_scope)
    if _eval_scope is not None:
        enabled_servers, enabled_mcp_keys, request.enabled_skill_ids = {}, [], []

    return (
        _desktop_prepared_servers,
        _prepared_capabilities,
        _progressive,
        _required_connector_server_keys,
        _required_plugin_server_keys,
        _subagent_progressive,
        enabled_mcp_keys,
        enabled_servers,
    )
