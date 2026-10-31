"""Translate neutral context additions at the framework boundary."""
from core.harness.events import thaw_value
from core.llm.context.items import ContextItem
from core.llm.context.ir_constants import POLICY_HEAD_TAIL
from core.llm.context.rendering import append_state_context_item, next_context_sequence
from core.llm.runtime.turn_state import reset_artifact_read_state, reset_pin_hint_state


def attachment_input(agent):
    state = getattr(agent, "state", None)
    return {name: getattr(state, name, default) for name, default in (
        ("uploaded_files", []), ("historical_files", []), ("user_id", ""),
        ("model_provider_id", ""), ("vision_evidence_text", ""),
    )}


def begin_context_turn():
    reset_artifact_read_state()
    reset_pin_hint_state()


def append_hook_context(agent, invocation):
    appended = []
    for item in invocation.data.get("context_items", ()):
        item = thaw_value(item)
        sequence = next_context_sequence(agent.state.context)
        origin = item["origin"]
        context_item = ContextItem.create(
            item_id=f"{origin}:{sequence}", kind=item["kind"], origin=origin,
            trust=item["trust"], visibility="model", priority=item["priority"],
            token_budget=item["token_budget"], truncation_policy=item.get("truncation_policy", POLICY_HEAD_TAIL),
            content=item["content"], cache_class="dynamic", created_seq=sequence,
            render_role="user", message_group=f"{origin}:{sequence}",
        )
        append_state_context_item(agent.state, context_item)
        appended.append(context_item)
    return appended


# A steer is persisted as its own user row by the chat runtime; recording the
# injection as well would replay the instruction twice.
_NOT_RECORDED_KINDS = frozenset({"steer"})


async def record_hook_context(agent, items, *, before_input):
    """Keep injected blocks in the turn's step record so the next turn replays them.

    Only the reply and reasoning stages call this: their blocks sit at a clear
    position (before the user's message / before a model call). Tool-stage
    injections can land between a call and its result and are not replayed.
    """
    import asyncio
    from core.llm.runtime.model_steps import record_context_step

    ledger = getattr(getattr(agent, "state", None), "pending_model_steps", None)
    if ledger is None:
        return
    for item in items:
        if item.kind in _NOT_RECORDED_KINDS:
            continue
        if isinstance(item.content, (list, tuple)):  # may carry media → blob table write
            step = await asyncio.to_thread(record_context_step, item, before_input=before_input)
        else:
            step = record_context_step(item, before_input=before_input)
        ledger.append(step)


async def apply_reasoning_hooks(adapter, agent, input_kwargs, plain, restore_context):
    from agentscope.tool import ToolChoice
    from core.harness.hooks import HookStage
    from core.llm.runtime.workspace import get_pinned_file_ids
    state = agent.state
    transformed = await adapter._stage(
        HookStage.TRANSFORM_CONTEXT, agent, operation_name="agent_reasoning",
        data={"identity": {"chat_id": getattr(state, "chat_id", None), "user_id": getattr(state, "user_id", None)},
              "context": plain(state.context), "context_items": [],
              "tool_choice": plain(input_kwargs.get("tool_choice")),
              "runtime": {"pinned_file_ids": list(get_pinned_file_ids()), "cur_iter": getattr(state, "cur_iter", 0),
                          "reply_id": getattr(state, "reply_id", ""),
                          "max_iters": getattr(getattr(agent, "react_config", None), "max_iters", 0)}},
    )
    if transformed.data.get("context") is not None:
        state.context = restore_context(state.context, transformed.data["context"])
    await record_hook_context(agent, append_hook_context(agent, transformed), before_input=False)
    await adapter._stage(HookStage.AFTER_CONTEXT, agent, operation_name="agent_reasoning",
                         data={"steer_delivery": transformed.data.get("steer_delivery")})
    if transformed.data.get("steer_delivery") is not None:
        from core.harness.events import thaw_value
        state.steer_delivery = thaw_value(transformed.data["steer_delivery"])
    choice = transformed.data.get("tool_choice")
    if choice is not None:
        from core.harness.events import thaw_value
        return {**input_kwargs, "tool_choice": ToolChoice.model_validate(thaw_value(choice))}
    return input_kwargs


async def finish_context_hooks(adapter, agent, output, plain):
    from core.harness.hooks import HookStage
    from core.llm.runtime.workspace import get_pinned_file_ids
    result = await adapter._stage(HookStage.BEFORE_FINISH, agent, operation_name="agent_reply",
        data={"output": plain(output), "metadata": {},
              "identity": {"chat_id": getattr(agent.state, "chat_id", None), "user_id": getattr(agent.state, "user_id", None)},
              "runtime": {"pinned_file_ids": list(get_pinned_file_ids()),
                          "finish_pins_completed": getattr(adapter, "finish_pins_completed", False)}})
    if result.data.get("continue_run"):
        return True
    pins = result.data.get("pin_file_ids") or ()
    if pins:
        from core.llm.runtime.finish_guard import pin_files
        if pin_files(list(pins)) > 0:
            adapter.finish_pins_completed = True
    return False
