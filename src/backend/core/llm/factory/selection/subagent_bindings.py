"""Agent assembly phase: subagent bindings. """

from __future__ import annotations

import asyncio
from dataclasses import replace

import prompts.prompt_config as prompt_config
from core.capabilities.paths import capabilities_enabled
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def subagent_bindings(request: AgentRequest, *, _api_scope, _elapsed, _log):
    cfg = prompt_config.load_prompt_config()
    _log.info("[factory] +%s config loaded", _elapsed())
    if request.agent_spec is not None and request.agent_spec.prompt_parts:
        cfg = replace(
            cfg,
            system_prompt=replace(cfg.system_prompt, parts=list(request.agent_spec.prompt_parts)),
        )

    # ── Sub-agent overrides ──────────────────────────────────────────
    # _subagent_progressive: the sub-agent's bound plugins, progressively
    # loaded — deferred here, directory + load_plugin injected in the subagent
    # prompt branch below. Activation is in-run only (no sticky persistence:
    # sub-agent runs are short-lived and isolated, and writing under the parent
    # chat's key would leak the activation into the main agent's assembly).
    _subagent_progressive = None
    if request.user_agent is not None:
        # Override capability bindings from user_agent config
        request.enabled_mcp_ids = list(request.user_agent.mcp_server_ids or [])
        request.enabled_skill_ids = factory_capabilities._device_available_skill_ids(
            list(request.user_agent.skill_ids or []),
            plugin_ids=list(request.user_agent.plugin_ids or []),
            user_id=request.current_user_id,
        )
        # Keep the definition the capability runtime inspects in sync with the
        # device-available set: preflight re-derives dependencies from the agent
        # definition, so a bound-but-unavailable skill must be dropped here too,
        # otherwise it re-raises DependencyMissing for a skill this device lacks.
        if list(request.user_agent.skill_ids or []) != request.enabled_skill_ids:
            import dataclasses as _dc

            request.user_agent = _dc.replace(
                request.user_agent, skill_ids=list(request.enabled_skill_ids)
            )
        request.enabled_kb_ids = request.user_agent.kb_ids or []
        # Expand bound plugins into their component skills + MCPs (a plugin = a detachable capability bundle). Merge with the loose bindings, deduplicated.
        plugin_ids = request.user_agent.plugin_ids or []
        if plugin_ids:
            from core.plugins import runtime as _plug_sub

            if (
                not request.disable_tools
                and not capabilities_enabled()
                and _api_scope is None
                and _plug_sub.progressive_plugin_loading_enabled()
            ):
                try:

                    def _resolve_bound():
                        # Same ownership/release narrowing the eager expansion
                        # would have received via factory_capabilities._filter_skill_ids_for_user.
                        return _plug_sub.resolve_bound_progressive_plugins(
                            list(plugin_ids),
                            skill_filter=lambda sids: factory_capabilities._filter_skill_ids_for_user(
                                sids, request.current_user_id
                            ),
                        )

                    _subagent_progressive = await asyncio.to_thread(_resolve_bound)
                    if not _subagent_progressive.directory:
                        _subagent_progressive = None
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "[factory] subagent progressive plugin resolve failed（回退全量装配）: %s",
                        exc,
                    )
                    _subagent_progressive = None
            p_skills, p_mcp = factory_capabilities._expand_plugin_bindings(
                plugin_ids, user_id=request.current_user_id
            )
            if _subagent_progressive is not None:
                # Deferred components stay out of the assembly; stdio-transport
                # plugins (absent from deferred_*) still expand eagerly.
                p_skills = [
                    s for s in p_skills if s not in _subagent_progressive.deferred_skill_ids
                ]
                p_mcp = [m for m in p_mcp if m not in _subagent_progressive.deferred_mcp_ids]
            request.enabled_skill_ids = list(dict.fromkeys(request.enabled_skill_ids + p_skills))
            request.enabled_mcp_ids = list(dict.fromkeys(request.enabled_mcp_ids + p_mcp))

    return (
        _subagent_progressive,
        cfg,
    )
