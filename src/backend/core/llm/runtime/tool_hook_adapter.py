"""Translate neutral tool decisions; arguments remain immutable after admission."""
from agentscope.tool._response import ToolResponse
from core.harness.events import thaw_value
from core.harness.hooks import DecisionKind, HookRejected, HookPaused, HookStage, Invocation
from core.llm.runtime.context_hook_adapter import append_hook_context


async def _tool_admission(adapter, agent, tool_call, args, plain):
    state = agent.state
    outcome = await adapter.bus.run(Invocation.create(
        run_id=adapter._run_id(agent), stage=HookStage.BEFORE_TOOL,
        operation_name=str(getattr(tool_call, "name", "") or "tool"),
        data={"tool_args": args, "raw_input": plain(getattr(tool_call, "input", None)),
              "ontology_runtime": plain(getattr(state, "ontology_runtime", None)),
              "identity": {"journal_owner": getattr(state, "journal_owner", ""), "user_id": getattr(state, "user_id", ""), "chat_id": getattr(state, "chat_id", "")},
              "context_items": [], "metadata": {"tool_call_id": getattr(tool_call, "id", "")}},
    ))
    updated = outcome.invocation
    if updated.data.get("ontology_runtime") is not None:
        state.ontology_runtime = thaw_value(updated.data["ontology_runtime"])
        state.ontology_enabled = bool(state.ontology_runtime.get("enabled"))
    append_hook_context(agent, updated)
    response = updated.data.get("tool_response")
    if outcome.decision.kind == DecisionKind.REJECT:
        if response is None:
            raise HookRejected(outcome.decision.reason)
        return ToolResponse.model_validate(thaw_value(response))
    if outcome.decision.kind == DecisionKind.PAUSE:
        raise HookPaused(outcome.decision.reason)
    return None


async def permission_dispatch(adapter, agent, input_kwargs, next_handler):
    from contextlib import aclosing, nullcontext
    call_id = str(getattr(input_kwargs.get("tool_call"), "id", "") or "")
    policy = adapter.permission_policy
    scope = policy.bind(call_id) if policy is not None else nullcontext(None)
    async def dispatch(**kwargs):
        async with aclosing(execute_tool(adapter, agent, kwargs, next_handler)) as executed:
            async for event in executed:
                yield event
    with scope as audit:
        async with aclosing(dispatch(**input_kwargs)) as stream:
            async for item in stream:
                if isinstance(item, ToolResponse) and audit is not None:
                    item.metadata = {**dict(item.metadata or {}), "permission": audit}
                yield item


async def tool_admission(adapter, agent, tool_call, args, plain):
    call_id = str(getattr(tool_call, "id", "") or "")
    try:
        result = await _tool_admission(adapter, agent, tool_call, args, plain)
    except BaseException:
        if adapter.permission_policy is not None:
            adapter.permission_policy.discard(call_id)
        raise
    if result is not None and adapter.permission_policy is not None:
        adapter.permission_policy.discard(call_id)
    return result


async def execute_tool(adapter, agent, input_kwargs, next_handler):
    import asyncio
    from contextlib import aclosing
    from core.harness.tool_call_context import CURRENT_TOOL_CALL_ID
    call = input_kwargs.get("tool_call")
    call_id = str(getattr(call, "id", "") or "")
    prepared = getattr(agent, "_jx_prepared_capabilities", None)
    if prepared is not None:
        await asyncio.to_thread(prepared.validate, user_id=str(getattr(agent.state, "user_id", "") or ""))
        await asyncio.to_thread(prepared.record_tool_scope, call_id, str(getattr(call, "name", "") or ""))
    token = CURRENT_TOOL_CALL_ID.set(call_id)
    try:
        stream = (adapter.tool_dispatch.stream(agent, input_kwargs, next_handler)
                  if adapter.tool_dispatch is not None else next_handler(**input_kwargs))
        async with aclosing(stream):
            async for item in stream:
                yield item
    finally:
        CURRENT_TOOL_CALL_ID.reset(token)
