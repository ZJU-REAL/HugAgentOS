"""Agent assembly phase: progressive bindings. """

from __future__ import annotations

import asyncio

import core.config.catalog as catalog
from core.capabilities.paths import capabilities_enabled
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def progressive_bindings(request: AgentRequest, *, _eval_scope, _log):
    if capabilities_enabled() and not request.disable_tools and request.enabled_skill_ids:
        from core.capabilities.preparation import ensure_cloud_ready

        await asyncio.to_thread(
            ensure_cloud_ready, request.current_user_id, skill_keys=request.enabled_skill_ids
        )

    # Security: strip out other users' private skills, preventing unauthorized skill_ids passed in from the frontend
    if request.enabled_skill_ids:
        request.enabled_skill_ids = factory_capabilities._filter_skill_ids_for_user(
            request.enabled_skill_ids, request.current_user_id
        )

    # ── Progressive plugin loading（渐进式插件加载）────────────────────────
    # Main-path only: a sub-agent's plugin binding is the owner's deliberate
    # configuration and a restricted mode's plugin set is the admin's deliberate
    # narrowing — both stay eager. Here, installed plugins' components are
    # removed from this run's enabled sets and replaced by a one-line directory
    # entry + a `load_plugin` activation tool (see core/plugins/runtime/__init__.py).
    # Deferral happens BEFORE the skill-bound-MCP merge below so a deferred
    # skill doesn't pull its bound MCP servers into the assembly either.
    _progressive = None
    if (
        not request.disable_tools
        and not request.turbo_mode
        and request.user_agent is None
        and request.current_user_id
        and _eval_scope is None
    ):
        from core.plugins import runtime as _plug

        if not capabilities_enabled() and _plug.progressive_plugin_loading_enabled():
            try:
                # Normalize the None fallbacks to their concrete resolutions
                # (identical sources to the later phases) so the subtraction
                # below has explicit lists to operate on.
                if request.enabled_skill_ids is None:
                    request.enabled_skill_ids = factory_capabilities._filter_skill_ids_for_user(
                        factory_capabilities._effective_main_available_skills(),
                        request.current_user_id,
                    )
                if not isinstance(request.enabled_mcp_ids, list):
                    request.enabled_mcp_ids = [
                        x
                        for x in catalog.get_enabled_ids("mcp")
                        if isinstance(x, str) and x.strip()
                    ]
                _progressive = await asyncio.to_thread(
                    _plug.resolve_progressive_plugins,
                    user_id=str(request.current_user_id),
                    chat_id=request.chat_id,
                    enabled_skill_ids=request.enabled_skill_ids,
                    enabled_mcp_ids=request.enabled_mcp_ids,
                    invoked_skill_ids=request.invoked_skill_ids,
                    invoked_mcp_ids=request.invoked_mcp_ids,
                )
                if _progressive.deferred_skill_ids:
                    request.enabled_skill_ids = [
                        s
                        for s in request.enabled_skill_ids
                        if s not in _progressive.deferred_skill_ids
                    ]
                if _progressive.deferred_mcp_ids:
                    request.enabled_mcp_ids = [
                        m for m in request.enabled_mcp_ids if m not in _progressive.deferred_mcp_ids
                    ]
                if not _progressive.directory:
                    _progressive = None
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "[factory] progressive plugin resolve failed（回退全量装配）: %s",
                    exc,
                )
                _progressive = None

    # A skill's MCP binding is an explicit capability grant, just like a sub-agent binding.
    # Merge only the MCPs declared by enabled skills; unrelated disabled MCPs remain disabled.
    skill_ids_for_bindings = (
        request.enabled_skill_ids
        if request.enabled_skill_ids is not None
        else factory_capabilities._effective_main_available_skills()
    )
    skill_bound_mcp_ids = (
        factory_capabilities._mcp_ids_bound_to_skills(skill_ids_for_bindings)
        if not request.disable_tools
        else []
    )
    if skill_bound_mcp_ids:
        base_mcp_ids = (
            request.enabled_mcp_ids
            if isinstance(request.enabled_mcp_ids, list)
            else [item for item in catalog.get_enabled_ids("mcp") if isinstance(item, str)]
        )
        request.enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *skill_bound_mcp_ids]))

    # Security: strip out KBs the current user has no access to (public-KB
    # permission assignment) — the frontend-supplied enabled_kb_ids may include
    # unauthorized scoped KBs; filter by the user's visible set here so the
    # agent only retrieves from authorized KBs.
    if request.enabled_kb_ids:
        request.enabled_kb_ids = factory_capabilities._filter_kb_ids_for_user(
            request.enabled_kb_ids, request.current_user_id
        )

    # ── Orchestration profile (the assembly this task type calls for) ───────
    # Resolved per run, per subject. This is the only place the profile is read,
    # so everything it governs — which tools are visible, which skills are
    # offered, what the retrieval budget is — is decided once, here, from one
    # reviewed artefact rather than from constants scattered across the module.

    return (
        _progressive,
        skill_bound_mcp_ids,
        skill_ids_for_bindings,
    )
