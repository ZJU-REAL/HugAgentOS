"""The only AgentScope-specific translation layer for the neutral HookBus."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolResponse

from core.harness.events import Event, EventSink, thaw_value
from core.harness.hooks import HookBus, HookStage, Invocation
from core.harness.usage import (
    UsageAttempt,
    attempt_status_for_exception,
    record_usage_safely,
)
from core.services.harness_ledger import DurableEventStore, HarnessUsageLedger


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
        legacy_middlewares: tuple[MiddlewareBase, ...] = (),
    ) -> None:
        self.bus = bus or build_hook_bus()
        # Transitional policies execute only through this compatibility
        # boundary. They are no longer mounted beside the adapter in Agent.
        self.legacy_middlewares = tuple(legacy_middlewares)

    def _legacy_for(self, hook_name: str) -> tuple[MiddlewareBase, ...]:
        return tuple(
            middleware
            for middleware in self.legacy_middlewares
            if middleware.is_implemented(hook_name)
        )

    async def _record_legacy_hook(
        self,
        *,
        run_id: str,
        middleware: MiddlewareBase,
        hook_name: str,
        status: str,
        started: float,
    ) -> None:
        if not run_id or self.bus.usage_recorder is None:
            return
        await record_usage_safely(
            self.bus.usage_recorder,
            UsageAttempt(
                run_id=run_id,
                kind="hook",
                operation_name=f"{type(middleware).__name__}.{hook_name}",
                status=status,
                latency_ms=int((time.monotonic() - started) * 1_000),
                metadata={"adapter": "agentscope", "legacy": True},
            ),
        )

    async def _legacy_stream(
        self,
        hook_name: str,
        agent: Any,
        input_kwargs: dict,
        next_handler,
    ):
        middlewares = self._legacy_for(hook_name)

        async def execute(index: int, **kwargs):
            if index >= len(middlewares):
                async for item in next_handler(**kwargs):
                    yield item
                return
            middleware = middlewares[index]

            async def following(**next_kwargs):
                async for item in execute(index + 1, **next_kwargs):
                    yield item

            callback = getattr(middleware, hook_name)
            started = time.monotonic()
            status = "success"
            try:
                async for item in callback(agent, kwargs, following):
                    yield item
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = attempt_status_for_exception(exc)
                raise
            finally:
                await self._record_legacy_hook(
                    run_id=self._run_id(agent),
                    middleware=middleware,
                    hook_name=hook_name,
                    status=status,
                    started=started,
                )

        async for item in execute(0, **input_kwargs):
            yield item

    async def _legacy_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler,
    ):
        middlewares = self._legacy_for("on_model_call")

        async def execute(index: int, **kwargs):
            if index >= len(middlewares):
                return await next_handler(**kwargs)
            middleware = middlewares[index]

            async def following(**next_kwargs):
                return await execute(index + 1, **next_kwargs)

            started = time.monotonic()
            status = "success"
            try:
                return await middleware.on_model_call(agent, kwargs, following)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = attempt_status_for_exception(exc)
                raise
            finally:
                await self._record_legacy_hook(
                    run_id=self._run_id(agent),
                    middleware=middleware,
                    hook_name="on_model_call",
                    status=status,
                    started=started,
                )

        return await execute(0, **input_kwargs)

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
        await self.bus.event_sink.append(
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
        from core.llm.model_usage import model_usage_scope

        run_id = self._run_id(agent)
        with model_usage_scope(run_id, self.bus.usage_recorder):
            await self._stage(
                HookStage.BEFORE_RUN,
                agent,
                operation_name="agent_reply",
                data={"metadata": {"framework": "agentscope"}},
            )
            transformed = await self._stage(
                HookStage.TRANSFORM_CONTEXT,
                agent,
                operation_name="agent_reply",
                data={
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
            final_output = None
            async for event in self._legacy_stream(
                "on_reply", agent, invoke_kwargs, next_handler
            ):
                await self._observe_event(event, run_id)
                if isinstance(event, Msg):
                    if final_output is not None:
                        yield final_output
                    final_output = event
                else:
                    yield event
            await self._stage(
                HookStage.BEFORE_FINISH,
                agent,
                operation_name="agent_reply",
                data={"output": _plain(final_output), "metadata": {}},
            )
            if final_output is not None:
                yield final_output

    async def on_reasoning(self, agent: Any, input_kwargs: dict, next_handler):
        from core.llm.model_usage import model_usage_scope

        with model_usage_scope(self._run_id(agent), self.bus.usage_recorder):
            async for event in self._legacy_stream(
                "on_reasoning", agent, input_kwargs, next_handler
            ):
                yield event

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
        response = await self._legacy_model_call(agent, input_kwargs, next_handler)
        if not inspect.isasyncgen(response):
            await self._stage(
                HookStage.AFTER_MODEL,
                agent,
                operation_name=model_name or "model",
                data={"response": _plain(response), "metadata": {}},
            )
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
            if pending is not None:
                yield pending

        return patched_stream()

    async def on_acting(self, agent: Any, input_kwargs: dict, next_handler):
        tool_call = input_kwargs.get("tool_call")
        raw_args = getattr(tool_call, "input", "") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (TypeError, json.JSONDecodeError):
            args = {"_raw": str(raw_args)}
        name = str(getattr(tool_call, "name", "") or "tool")
        await self._stage(
            HookStage.BEFORE_TOOL,
            agent,
            operation_name=name,
            data={"tool_args": args, "metadata": {}},
        )
        final = None
        async for event in self._legacy_stream(
            "on_acting", agent, input_kwargs, next_handler
        ):
            if isinstance(event, ToolResponse):
                final = event
            else:
                yield event
        await self._stage(
            HookStage.AFTER_TOOL,
            agent,
            operation_name=name,
            data={"tool_result": _plain(final), "metadata": {}},
        )
        if final is not None:
            yield final
