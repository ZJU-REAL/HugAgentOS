"""Agent assembly phase: middleware stack. """

from __future__ import annotations

from core.llm.agentscope_hook_adapter import AgentScopeHookAdapter
from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime import evidence as factory_evidence_helpers
from core.llm.middlewares import (
    ActingToolCallIdMiddleware,
    AgentRuntimeState,
    CitationAnchorMiddleware,
    DynamicModelMiddleware,
    FileContextMiddleware,
    FinishPinGuardMiddleware,
    IterBudgetReminderMiddleware,
    JobLedgerReminderMiddleware,
    OntologyGateMiddleware,
    PlanStaleReminderMiddleware,
    StallInterventionMiddleware,
    SteerMiddleware,
    ToolEffectMiddleware,
    WorkspacePinHintMiddleware,
)


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
    from core.llm.tool_permissions import (
        PermissionRuntime,
        ToolPermissionMiddleware,
        ToolPermissionRegistry,
        ToolPermissionService,
    )

    _builtin_tool_names = {ft.name for ft in toolkit.function_tools}
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
            source="native",
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
        ),
    )

    # AgentScope's native permission engine remains a coarse first gate. Names
    # present in the registry additionally pass ToolPermissionMiddleware and
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

    _policy_middlewares: list = [
        DynamicModelMiddleware(),  # on_reply: switch models by chat_mode
        FileContextMiddleware(),  # on_reply: inject file context
        SteerMiddleware(),  # on_acting/on_reasoning: inject queued user steer before tool I/O
        WorkspacePinHintMiddleware(),  # on_reasoning: remind to pin
        IterBudgetReminderMiddleware(),  # on_reasoning: inject a wrap-up reminder near max_iters
        # on_acting: the active profile's intervention rules, applied to *this*
        # loop. Previously they only reached the autonomous loop, which left the
        # orchestration profile with one field that governed nothing on the axis
        # almost all traffic takes.
        StallInterventionMiddleware(profile.intervention_rules),
        OntologyGateMiddleware(_ontology_runtime),  # on_acting: zero-LLM L-a contract gate
        ToolPermissionMiddleware(_permission_service),
        CitationAnchorMiddleware(),  # on_acting: 证据锚点——工具结果回给模型前发号回注 cite_id
        ActingToolCallIdMiddleware(),  # on_acting: expose call_subagent's tool_call.id to tools (parent-child linkage)
        ToolEffectMiddleware(),  # on_acting: durable Intent before every actual tool invocation
    ]
    # on_reasoning: 会话里有未收敛的批量作业时，每轮把台账数字回灌进上下文。
    # 进度是外部事实（job_items 表），不是模型的记忆——不主动回灌，隔十几轮之后就会
    # 退化成"边际收益递减，先交付吧"（568 行只补 66 行正是这么停的）。
    # 子作业内部不挂（isolated），避免嵌套噪声。
    if request.workflow_mode and not request.isolated and request.chat_id:
        _policy_middlewares.append(
            JobLedgerReminderMiddleware(chat_id=request.chat_id, user_id=request.current_user_id)
        )
    # on_reasoning: 计划栏停在半路时把当前清单回灌回去，催模型调 update_plan。
    # 只在真的注册了 update_plan 工具的那条路径上挂（同一个正向开关），派生/非交互
    # 的构造一律拿不到——催一个不存在的工具只会让模型编造调用。
    if _plan_tool_enabled:
        _policy_middlewares.append(PlanStaleReminderMiddleware())
    # The legacy guard directly pins ids found in arbitrary tool output. API
    # delivery must go through the scoped tool's ownership check and persistence.
    if _api_scope is None and _eval_scope is None:
        _policy_middlewares.append(FinishPinGuardMiddleware(batch_mode=request.batch_mode))
    # The Agent sees one framework adapter. Transitional AgentScope policies
    # execute inside its compatibility chain and can be deleted one by one as
    # their neutral HookSpec replacements reach parity.
    _middlewares: list = [AgentScopeHookAdapter(legacy_middlewares=tuple(_policy_middlewares))]

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
