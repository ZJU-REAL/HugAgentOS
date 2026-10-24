"""The only AgentScope-specific translation layer for the neutral HookBus."""

from __future__ import annotations

import inspect
from typing import Any

from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase
from core.harness.events import Event, EventSink, thaw_value
from core.harness.hooks import HookBus, HookStage, Invocation
from core.llm.runtime.model_steps import record_assistant_step
from core.models.reasoning_replay import stamp_reasoning_origin
from core.llm.runtime.context_hook_adapter import attachment_input, begin_context_turn, append_hook_context, apply_reasoning_hooks
from core.llm.runtime.model_selection_adapter import apply_model_selection, model_selection_input
from core.harness.ledger import DurableEventStore, HarnessUsageLedger


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(mode="json"))
    return str(value)


def _restore_like(original: Any, value: Any) -> Any:
    value = thaw_value(value)
    if original is None or value is None:
        return value
    if isinstance(original, Msg):
        if isinstance(value, list):
            return [
                Msg.model_validate(item) if isinstance(item, dict) else item
                for item in value
            ]
        if isinstance(value, dict):
            return Msg.model_validate(value)
    if isinstance(original, list) and isinstance(value, (list, tuple)):
        message_template = next(
            (item for item in original if isinstance(item, Msg)),
            None,
        )
        return [
            _restore_like(original[index], item)
            if index < len(original)
            else (
                Msg.model_validate(item)
                if message_template is not None and isinstance(item, dict)
                else item
            )
            for index, item in enumerate(value)
        ]
    validate = getattr(type(original), "model_validate", None)
    if callable(validate) and isinstance(value, dict):
        return validate(value)
    return value


def _restore_messages(original: Any, value: Any) -> Any:
    """Restore neutral message payloads even when the original input was empty."""
    value = thaw_value(value)
    if value is None:
        return None
    if isinstance(value, list):
        return [
            Msg.model_validate(item) if isinstance(item, dict) else item
            for item in value
        ]
    if isinstance(value, dict):
        return Msg.model_validate(value)
    return _restore_like(original, value)


def _restore_context(original: Any, value: Any) -> Any:
    """Restore message-shaped context without coercing arbitrary dictionaries."""
    value = thaw_value(value)
    if isinstance(value, list):
        return [
            Msg.model_validate(item)
            if isinstance(item, dict) and {"role", "content"}.issubset(item)
            else item
            for item in value
        ]
    return _restore_like(original, value)


def adapt_agentscope_event(event: Any, *, run_id: str) -> Invocation | None:
    """Translate public/fake framework events without leaking their classes."""
    name = type(event).__name__
    if name == "ModelCallStartEvent":
        return Invocation.create(
            run_id=run_id,
            stage=HookStage.BEFORE_MODEL,
            operation_name=str(getattr(event, "model_name", "") or "model"),
            data={"model": getattr(event, "model_name", "") or ""},
            metadata={"framework_event": name},
        )
    if name == "ModelCallEndEvent":
        return Invocation.create(
            run_id=run_id,
            stage=HookStage.AFTER_MODEL,
            operation_name="model",
            data={
                "response": {
                    "prompt_tokens": int(getattr(event, "input_tokens", 0) or 0),
                    "completion_tokens": int(getattr(event, "output_tokens", 0) or 0),
                }
            },
            metadata={"framework_event": name},
        )
    if name in {"ToolCallStartEvent", "ToolCallEndEvent"}:
        return Invocation.create(
            run_id=run_id,
            stage=HookStage.BEFORE_TOOL,
            operation_name=str(getattr(event, "tool_call_name", "") or "tool"),
            data={
                "tool_args": {},
                "metadata": {"tool_call_id": getattr(event, "tool_call_id", "") or ""},
            },
            metadata={"framework_event": name},
        )
    if name == "ToolResultEndEvent":
        return Invocation.create(
            run_id=run_id,
            stage=HookStage.AFTER_TOOL,
            operation_name=str(getattr(event, "tool_call_name", "") or "tool"),
            data={
                "tool_result": {
                    "state": str(getattr(event, "state", "") or ""),
                },
                "metadata": {"tool_call_id": getattr(event, "tool_call_id", "") or ""},
            },
            metadata={"framework_event": name},
        )
    return None


def build_hook_bus() -> HookBus:
    """Create the production bus with durable append-only sinks."""
    return HookBus(
        event_sink=EventSink(DurableEventStore()),
        usage_recorder=HarnessUsageLedger(),
    )


class AgentScopeHookAdapter(MiddlewareBase):
    """Translate AgentScope middleware callbacks into stable hook stages."""

    def __init__(
        self,
        bus: HookBus | None = None,
        *,
        permission_policy=None,
        durable_tools=False,
    ) -> None:
        self.bus = bus or build_hook_bus()
        self.permission_policy = permission_policy
        from core.llm.runtime.tool_execution_adapter import DurableToolDispatch
        self.tool_dispatch = DurableToolDispatch() if durable_tools else None

    @staticmethod
    def _run_id(agent: Any) -> str:
        return str(getattr(getattr(agent, "state", None), "run_id", "") or "")

    async def _stage(
        self,
        stage: HookStage,
        agent: Any,
        *,
        operation_name: str,
        data: dict[str, Any] | None = None,
    ) -> Invocation:
        return await self.bus.enforce(
            Invocation.create(
                run_id=self._run_id(agent),
                stage=stage,
                operation_name=operation_name,
                data=data or {},
            )
        )

    async def _observe_event(self, event: Any, run_id: str) -> None:
        invocation = adapt_agentscope_event(event, run_id=run_id)
        if invocation is None:
            return
        await self.bus.event_sink.publish(
            Event.create(
                run_id=run_id,
                event_type="framework_invocation",
                phase=invocation.stage.value,
                payload={
                    "operation_name": invocation.operation_name,
                    "data": invocation.data,
                    "metadata": invocation.metadata,
                },
            )
        )

    async def on_reply(self, agent: Any, input_kwargs: dict, next_handler):
        from core.models.usage import model_usage_scope

        run_id = self._run_id(agent)
        with model_usage_scope(run_id, self.bus.usage_recorder):
            selected = await self._stage(
                HookStage.BEFORE_RUN,
                agent,
                operation_name="agent_reply",
                data={"metadata": {"framework": "agentscope"}, "runtime": model_selection_input(agent)},
            )
            apply_model_selection(agent, selected)
            begin_context_turn()
            transformed = await self._stage(
                HookStage.TRANSFORM_CONTEXT,
                agent,
                operation_name="agent_reply",
                data={
                    "runtime": _plain(attachment_input(agent)),
                    "context_items": [],
                    "messages": _plain(input_kwargs.get("inputs")),
                    "context": _plain(
                        getattr(getattr(agent, "state", None), "context", None)
                    ),
                },
            )
            invoke_kwargs = dict(input_kwargs)
            if (
                "messages" in transformed.data
                and transformed.data["messages"] is not None
            ):
                invoke_kwargs["inputs"] = _restore_messages(
                    input_kwargs.get("inputs"), transformed.data["messages"]
                )
            state = getattr(agent, "state", None)
            if state is not None and transformed.data.get("context") is not None:
                state.context = _restore_context(
                    getattr(state, "context", None), transformed.data["context"]
                )
            append_hook_context(agent, transformed)
            from core.llm.runtime.context_hook_adapter import finish_context_hooks
            while True:
                final_output = None
                async for event in next_handler(**invoke_kwargs):
                    await self._observe_event(event, run_id)
                    if isinstance(event, Msg):
                        if final_output is not None:
                            yield final_output
                        final_output = event
                    else:
                        yield event
                continue_run = await finish_context_hooks(self, agent, final_output, _plain)
                if final_output is not None:
                    yield final_output
                if not continue_run:
                    break
                invoke_kwargs = {**invoke_kwargs, "inputs": None}

    async def on_reasoning(self, agent: Any, input_kwargs: dict, next_handler):
        from core.models.usage import model_usage_scope

        with model_usage_scope(self._run_id(agent), self.bus.usage_recorder):
            input_kwargs = await apply_reasoning_hooks(self, agent, input_kwargs, _plain, _restore_context)
            async for event in next_handler(**input_kwargs):
                yield event

    @staticmethod
    def _record_model_step(agent: Any, current_model: Any, response: Any) -> None:
        """Keep the completed response as one canonical assistant step.

        ``_reasoning_impl`` saves ``completed_response.content`` into the
        context right after this hook returns, so recording the same blocks
        here is the model-facing truth of the step, tagged with the provider
        and wire protocol that produced it.
        """
        content = getattr(response, "content", None)
        if not getattr(response, "is_last", True) or content is None:
            return
        state = getattr(agent, "state", None)
        ledger = getattr(state, "pending_model_steps", None)
        if ledger is None:
            stamp_reasoning_origin(content, current_model)
            return
        ledger.append(
            record_assistant_step(
                list(content),
                provider=str(getattr(current_model, "provider_id", "") or ""),
                model=str(getattr(current_model, "model", "") or ""),
                protocol=str(getattr(current_model, "wire_protocol", "") or ""),
            )
        )
        stamp_reasoning_origin(content, current_model)

    async def on_model_call(self, agent: Any, input_kwargs: dict, next_handler):
        current_model = input_kwargs.get("current_model")
        model_name = str(getattr(current_model, "model", "") or "")
        parameters = {
            "tools": _plain(input_kwargs.get("tools")),
            "tool_choice": _plain(input_kwargs.get("tool_choice")),
        }
        await self._stage(
            HookStage.BEFORE_MODEL,
            agent,
            operation_name=model_name or "model",
            data={
                "model": model_name,
                "messages": _plain(input_kwargs.get("messages")),
                "parameters": parameters,
            },
        )
        response = await next_handler(**input_kwargs)
        if not inspect.isasyncgen(response):
            await self._stage(
                HookStage.AFTER_MODEL,
                agent,
                operation_name=model_name or "model",
                data={"response": _plain(response), "metadata": {}},
            )
            self._record_model_step(agent, current_model, response)
            return response

        async def patched_stream():
            pending = None
            async for item in response:
                if pending is not None:
                    yield pending
                pending = item
            await self._stage(
                HookStage.AFTER_MODEL,
                agent,
                operation_name=model_name or "model",
                data={"response": _plain(pending), "metadata": {}},
            )
            self._record_model_step(agent, current_model, pending)
            if pending is not None:
                yield pending

        return patched_stream()

    async def on_acting(self, agent: Any, input_kwargs: dict, next_handler):
        from contextlib import aclosing
        from core.llm.runtime.tool_result_adapter import run_tool
        async with aclosing(run_tool(self, agent, input_kwargs, next_handler, _plain)) as stream:
            async for event in stream:
                yield event
