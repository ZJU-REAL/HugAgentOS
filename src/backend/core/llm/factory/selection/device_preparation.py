"""Agent assembly phase: device preparation. """

from __future__ import annotations

import asyncio

import core.agent_skills.loader as skill_loader
from core.capabilities.paths import capabilities_enabled
from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def device_preparation(
    request: AgentRequest,
    *,
    _api_scope,
    _capability_run_key,
    _caps_skill_ids,
    _elapsed,
    _eval_scope,
    _log,
    _mode_plugin_ids,
    _note_unavailable,
    required: RequiredCapabilities,
    sticky: StickyCapabilities,
    enabled_mcp_keys,
):
    _desktop_progressive = None
    if (
        capabilities_enabled()
        and not request.disable_tools
        and _api_scope is None
        and _eval_scope is None
    ):
        from core.plugins import runtime as _desktop_plugins

        _desktop_progressive = await asyncio.to_thread(
            _desktop_plugins.resolve_desktop_progressive_plugins,
            user_id=str(request.current_user_id or ""),
            enabled_skill_ids=(
                request.enabled_skill_ids
                if request.enabled_skill_ids is not None
                else factory_capabilities._effective_main_available_skills()
            ),
            enabled_mcp_ids=[
                key
                for key in enabled_mcp_keys
                if request.enabled_mcp_ids is None or key in request.enabled_mcp_ids
            ],
            plugin_ids=list(request.user_agent.plugin_ids or []) if request.user_agent else None,
            activated_ids=[*sticky.plugin_ids, *_mode_plugin_ids, required.plugin_id],
            invoked_skill_ids=[
                *(request.invoked_skill_ids or []),
                *sticky.direct_skill_ids,
                required.skill_id,
            ],
            invoked_mcp_ids=[
                *(request.invoked_mcp_ids or []),
                *sticky.direct_mcp_ids,
                *required.connector_ids,
            ],
        )
        if _desktop_progressive.unavailable_skill_ids:
            request.enabled_skill_ids = [
                sid
                for sid in (
                    request.enabled_skill_ids
                    if request.enabled_skill_ids is not None
                    else factory_capabilities._effective_main_available_skills()
                )
                if sid not in _desktop_progressive.unavailable_skill_ids
            ]
        if (
            not _desktop_progressive.directory
            or request.turbo_mode
            or not _desktop_plugins.progressive_plugin_loading_enabled()
        ):
            _desktop_progressive = None

    from core.capabilities import runtime as capability_runtime

    _log.info("[factory] +%s progressive definitions resolved", _elapsed())

    _prepared_capabilities = None
    if capabilities_enabled() and not request.disable_tools:
        _caps_loader = skill_loader.get_skill_loader()
        _caps_skill_ids = (
            request.enabled_skill_ids
            if request.enabled_skill_ids is not None
            else factory_capabilities._effective_main_available_skills()
        )

        def _materialize_selected_skills():
            for _sid in _caps_skill_ids or []:
                from core.capabilities.errors import CapabilityError

                try:
                    _caps_loader.get_skill_dir(_sid)
                except (CapabilityError, OSError, ValueError):
                    _note_unavailable(f"技能「{_sid}」的本机文件暂不可用。")

        await asyncio.to_thread(_materialize_selected_skills)
        _log.info("[factory] +%s selected skill files ready", _elapsed())
        _dependency_plugins = [
            *list(getattr(request.user_agent, "plugin_ids", None) or []),
            *sticky.plugin_ids,
            *_mode_plugin_ids,
        ]
        if required.plugin_id:
            _dependency_plugins.append(required.plugin_id)
        _prepared_capabilities = await asyncio.to_thread(
            capability_runtime.prepare,
            _capability_run_key,
            str(request.current_user_id or ""),
            skill_ids=_caps_skill_ids,
            scope_id=request.capability_scope,
            agent_definition=request.user_agent,
            plugin_ids=list(dict.fromkeys(_dependency_plugins)),
        )

    return (
        _caps_skill_ids,
        _desktop_progressive,
        _prepared_capabilities,
    )
