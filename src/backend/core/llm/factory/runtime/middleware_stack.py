"""Agent assembly phase: middleware stack. """

from __future__ import annotations


from core.llm.runtime.agentscope_hook_adapter import AgentScopeHookAdapter
from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime import evidence as factory_evidence_helpers
from core.llm.runtime.state import AgentRuntimeState


async def middleware_stack(
    request: AgentRequest,
    *,
    _api_scope,
    _build_toolkit,
    _eval_scope,
    _interactive,
    _ontology_runtime,
    _plan_tool_enabled,
    _proj_scope,
    _sbx_sess,
    _subagent_model_pinned,
    _tool_approval_available,
    default_model,
    profile,
    tool_schemas,
    toolkit,
):
    from core.sandbox.permissions.dispatch_models import PermissionRuntime
    from core.sandbox.permissions.tool_specs import ToolPermissionRegistry
    from core.llm.tool_permissions import ToolPermissionService

    _builtin_tool_names = {ft.name for ft in list(toolkit)}
    _all_tool_names = {
        str(s.get("function", {}).get("name") or "")
        for s in tool_schemas
        if s.get("function", {}).get("name")
    } | _builtin_tool_names
    _permission_registry = ToolPermissionRegistry()
    for _tool_name, _permission_spec in toolkit.permission_specs.items():
        _permission_registry.register(
            _tool_name,
            _permission_spec,
            source="tool_collector",
        )
    _permission_service = ToolPermissionService(
        _permission_registry,
        PermissionRuntime(
            sandbox_session_id=_sbx_sess,
            project_scope=_proj_scope,
            chat_id=request.chat_id,
            user_id=request.current_user_id,
            interactive=_interactive,
            approval_available=_tool_approval_available,
            default_allow=bool(_api_scope or _eval_scope)
            or factory_evidence_helpers._default_allow_builtin_tools(
                channel_origin=request.channel_origin,
                automation_run=request.automation_run,
            ),
            approval_mode=request.approval_mode,
            read_only=request.read_only,
            account_approval=not bool(request.profile) and not bool(_api_scope or _eval_scope),
        ),
    )

    # AgentScope's native permission engine remains a coarse first gate. Names
    # present in the registry additionally pass the permission hook and
    # the final Toolkit ticket guard below.
    from agentscope.permission import PermissionContext

    _state = AgentRuntimeState(
        # The effective model name is read directly off the model object (the AS2 attribute is .model), same source as the compression window
        model_name=getattr(default_model, "model", None) or request.model_name or "",
        model_pinned=_subagent_model_pinned,
        model_provider_id=request.model_provider_id or "",
        chat_mode=request.chat_mode,
        user_id=request.current_user_id,
        chat_id=request.chat_id,
        run_id=request.run_id,
        journal_owner=request.journal_owner,
        capability_scope=request.capability_scope,
        ontology_enabled=bool(_ontology_runtime.get("enabled")),
        ontology_runtime=_ontology_runtime,
        permission_context=PermissionContext(),
    )

    from core.harness.registry import hook_specs

    from core.llm.tool_permissions.hook import ToolPermissionHook
    permission_policy = ToolPermissionHook(_permission_service)
    adapter = AgentScopeHookAdapter(permission_policy=permission_policy, durable_tools=True)
    from core.harness.registry.hook_factories import create_steering_policy
    steering = None
    if request.profile:
        from core.harness.registry import agent_profile
        selected_profile = agent_profile(request.profile)
        steering = selected_profile.steering(request)
        for spec in selected_profile.hooks(request):
            adapter.bus.register(spec)
    for spec in (steering or create_steering_policy()).specs():
        adapter.bus.register(spec)
    if request.project_environment is not None:
        from core.llm.runtime.project_environment_hook import project_environment_hook
        adapter.bus.register(project_environment_hook(request))
    if _eval_scope is None and _api_scope is None:
        from core.harness.registry.hook_factories import command_completion_hooks
        for spec in command_completion_hooks():
            adapter.bus.register(spec)
    adapter.bus.register(permission_policy.spec())
    from core.models.selection_hook import model_selection_hook
    adapter.bus.register(model_selection_hook())
    from core.llm.citations.hook import citation_hook
    adapter.bus.register(citation_hook())
    from core.ontology.tool_hook import ontology_tool_hooks
    for spec in ontology_tool_hooks(_ontology_runtime):
        adapter.bus.register(spec)
    from core.evolution.profiles.stall_hook import stall_hooks
    for spec in stall_hooks(profile.intervention_rules):
        adapter.bus.register(spec)
    from core.llm.context.attachment_hook import attachment_context_hook
    adapter.bus.register(attachment_context_hook())
    from core.llm.context.reasoning_hooks import iteration_budget_hook
    adapter.bus.register(iteration_budget_hook())
    from core.llm.context.workspace_hooks import workspace_pin_hooks, plan_stale_hook
    for spec in workspace_pin_hooks(allow_finish=_api_scope is None and _eval_scope is None):
        adapter.bus.register(spec)
    if _plan_tool_enabled:
        adapter.bus.register(plan_stale_hook())
    for spec in hook_specs(request):
        adapter.bus.register(spec)
    _middlewares: list = [adapter]

    # At this point the collector has gathered all tools (including any subagent tools) → construct the final Toolkit.
    toolkit = _build_toolkit()
    toolkit.set_tool_permission_service(_permission_service)

    # Allow all registered tools via native allow_rules (replacing BYPASS, see the explanation above).
    from agentscope.permission import PermissionBehavior, PermissionRule

    _state.permission_context.allow_rules = {
        n: [
            PermissionRule(
                tool_name=n,
                rule_content="",
                behavior=PermissionBehavior.ALLOW,
                source="jx_permission_manifest",
            )
        ]
        for n in _all_tool_names
    }

    return (
        _builtin_tool_names,
        _middlewares,
        _state,
        toolkit,
    )
