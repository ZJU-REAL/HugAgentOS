"""Agent assembly phase: sticky capabilities. """

from __future__ import annotations

import asyncio

import core.capabilities.catalog.api as catalog
from core.harness.registry.run_capabilities import has_run_capabilities
from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def sticky_capabilities(
    request: AgentRequest,
    *,
    _elapsed,
    _eval_scope,
    _log,
    _mode_manual_invoke,
    _note_unavailable,
    required: RequiredCapabilities,
    sticky: StickyCapabilities,
):
    # Dedicated profiles own their full binding list. Chat history must not widen it.
    from core.harness.registry import agent_profile
    explicit_profile = bool(request.profile and not agent_profile(request.profile).inherit_default_mcps)
    inherited_skills = set(request.enabled_skill_ids or [])
    inherited_mcps = set(request.enabled_mcp_ids or [])
    capability_chat = (
        request.capability_source_chat_id if request.derived_main else None
    ) or request.chat_id
    if (
        has_run_capabilities()
        and not request.disable_tools
        and not request.turbo_mode
        and request.user_agent is None
        and request.enabled_skill_ids is None
    ):
        from core.harness.registry.run_capabilities import plugin_skill_defaults

        request.enabled_skill_ids = await asyncio.to_thread(
            plugin_skill_defaults,
            request.current_user_id,
            factory_capabilities.effective_main_available_skills(),
        )

    _log.info("[factory] +%s default capabilities prepared", _elapsed())

    # A plugin explicitly selected for this turn is stronger than the default
    # catalog, a dedicated agent's saved bindings, and a restricted mode's
    # ordinary capability set. The API already enforced installation, ownership,
    # and dependency readiness; merge only those components so the execution guard
    # below has a real surface to require.
    if required.plugin_id:
        base_skill_ids = (
            list(request.enabled_skill_ids)
            if isinstance(request.enabled_skill_ids, list)
            else factory_capabilities.effective_main_available_skills()
        )
        request.enabled_skill_ids = list(
            dict.fromkeys([*base_skill_ids, *required.plugin_skill_ids])
        )
        base_mcp_ids = (
            list(request.enabled_mcp_ids)
            if isinstance(request.enabled_mcp_ids, list)
            else [item for item in catalog.get_enabled_ids("mcp") if isinstance(item, str)]
        )
        request.enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *required.plugin_mcp_ids]))

    if required.skill_id:
        base_skill_ids = (
            list(request.enabled_skill_ids)
            if isinstance(request.enabled_skill_ids, list)
            else factory_capabilities.effective_main_available_skills()
        )
        request.enabled_skill_ids = list(dict.fromkeys([*base_skill_ids, required.skill_id]))

    # A direct connector selection is a stronger per-turn user instruction
    # than the normal catalog/profile assembly. Keep the ordinary defaults and
    # add the selected connector; ownership/enabled-server filtering below is
    # still the final security boundary.
    if required.connector_ids:
        base_mcp_ids = (
            list(request.enabled_mcp_ids)
            if isinstance(request.enabled_mcp_ids, list)
            else [item for item in catalog.get_enabled_ids("mcp") if isinstance(item, str)]
        )
        request.enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *required.connector_ids]))

    # Successful explicit activation is a chat-level capability grant, not a
    # one-request hint. Restore plugins, direct skills and direct connectors
    # before progressive deferral and profile narrowing so later turns keep
    # exactly the same surface even while personal catalog switches remain off.
    # Both resolvers recheck visibility, admin state and dependencies each turn.
    if (
        not request.disable_tools
        and request.current_user_id
        and capability_chat
        and not explicit_profile
        and _eval_scope is None
        and (not request.turbo_mode or _mode_manual_invoke)
    ):
        from core.llm.runtime import session_capabilities as _sticky_direct
        from core.capabilities.plugins import runtime as _sticky_plugins

        try:
            _sticky, _direct = await asyncio.gather(
                asyncio.to_thread(
                    _sticky_plugins.resolve_sticky_plugin_capabilities,
                    user_id=str(request.current_user_id),
                    chat_id=capability_chat,
                ),
                asyncio.to_thread(
                    _sticky_direct.resolve_session_activated_capabilities,
                    user_id=str(request.current_user_id),
                    chat_id=capability_chat,
                ),
            )
            sticky.plugin_ids = list(_sticky.install_ids)
            for _unavailable_plugin in _sticky.unavailable_ids:
                _note_unavailable(f"此前使用的插件「{_unavailable_plugin}」已停用或暂不可用。")
            sticky.plugin_skill_ids = list(_sticky.skill_ids)
            sticky.plugin_mcp_ids = list(_sticky.mcp_ids)
            sticky.direct_skill_ids = list(_direct.skill_ids)
            sticky.direct_mcp_ids = list(_direct.mcp_ids)
            sticky_skill_ids = list(
                dict.fromkeys([*sticky.plugin_skill_ids, *sticky.direct_skill_ids])
            )
            sticky_mcp_ids = list(dict.fromkeys([*sticky.plugin_mcp_ids, *sticky.direct_mcp_ids]))
            if sticky_skill_ids:
                base_skill_ids = (
                    list(request.enabled_skill_ids)
                    if isinstance(request.enabled_skill_ids, list)
                    else factory_capabilities.effective_main_available_skills()
                )
                request.enabled_skill_ids = list(
                    dict.fromkeys([*base_skill_ids, *sticky_skill_ids])
                )
            if sticky_mcp_ids:
                base_mcp_ids = (
                    list(request.enabled_mcp_ids)
                    if isinstance(request.enabled_mcp_ids, list)
                    else [item for item in catalog.get_enabled_ids("mcp") if isinstance(item, str)]
                )
                request.enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *sticky_mcp_ids]))
            if _sticky.install_ids or sticky_skill_ids or sticky_mcp_ids:
                _log.info(
                    "[factory] sticky capabilities restored chat=%s installs=%s "
                    "skills=%d mcp=%d",
                    request.chat_id,
                    _sticky.install_ids,
                    len(sticky_skill_ids),
                    len(sticky_mcp_ids),
                )
        except Exception as exc:  # noqa: BLE001
            if has_run_capabilities():
                raise  # a saved source must not silently disappear or change account
            _log.warning("[factory] sticky capability restore failed: %s", exc)

    if request.derived_main:
        # A fork rechecks parent activation authority but never gains later grants.
        for name in ("direct_skill_ids", "plugin_skill_ids"):
            setattr(sticky, name, [s for s in getattr(sticky, name) if s in inherited_skills])
        for name in ("direct_mcp_ids", "plugin_mcp_ids"):
            setattr(sticky, name, [m for m in getattr(sticky, name) if m in inherited_mcps])
        request.enabled_skill_ids = [
            s for s in request.enabled_skill_ids or [] if s in inherited_skills
        ]
        request.enabled_mcp_ids = [m for m in request.enabled_mcp_ids or [] if m in inherited_mcps]
    return (sticky,)
