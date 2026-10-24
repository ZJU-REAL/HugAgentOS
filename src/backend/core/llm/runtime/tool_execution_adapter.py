"""SDK stream and payload translation for the injected durable tool executor."""
import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any
from agentscope.message import ToolResultState
from agentscope.tool._response import ToolChunk, ToolResponse
from core.harness.tool_call_context import CURRENT_RUN_BINDING
from core.llm.context.rendering import render_text_block

logger = logging.getLogger(__name__)


class DurableToolDispatch:
    """Commit durable Intent before every harness-owned tool invocation.

    The final ``ToolResponse`` is committed before it leaves this middleware,
    so the existing AgentScope event/SSE mapping remains unchanged while the
    database becomes authoritative for replay and reconciliation.
    """

    def __init__(self, session_factory=None, registry=None) -> None:  # noqa: ANN001
        from core.db.engine import SessionLocal
        from core.llm.ports import tool_effect_runtime

        self._gateway = tool_effect_runtime(session_factory, registry)
        from core.harness.ledger import HarnessUsageLedger

        self._usage_ledger = HarnessUsageLedger(session_factory or SessionLocal)

    @staticmethod
    def _args(tool_call) -> dict:  # noqa: ANN001
        raw = getattr(tool_call, "input", "") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            parsed = {"_raw": str(raw)}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}

    @staticmethod
    def _response_payload(response: ToolResponse) -> dict:
        return {"tool_response": response.model_dump(mode="json")}

    @staticmethod
    def _response_from_payload(payload: Any) -> ToolResponse:
        from agentscope.message import DataBlock, TextBlock

        raw = payload.get("tool_response") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return ToolResponse(
                content=[render_text_block(json.dumps(payload, ensure_ascii=False, default=str))],
                state=ToolResultState.SUCCESS,
            )
        blocks = []
        for item in raw.get("content") or []:
            if not isinstance(item, dict):
                blocks.append(render_text_block(str(item)))
            elif item.get("type") == "data":
                blocks.append(DataBlock.model_validate(item))
            else:
                blocks.append(TextBlock.model_validate(item))
        state = ToolResultState(str(raw.get("state") or "success"))
        return ToolResponse(
            id=str(raw.get("id") or ""),
            content=blocks,
            state=state,
            metadata=dict(raw.get("metadata") or {}),
        )

    async def stream(self, agent: Any, input_kwargs: dict, next_handler):  # noqa: ANN001
        run_id = str(getattr(agent.state, "run_id", "") or "")
        run_owner = str(getattr(agent.state, "journal_owner", "") or "")
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if not run_id or not run_owner or not tool_name:
            raise self._gateway.binding_error(
                "tool execution requires a durable run_id and journal_owner binding"
            )

        args = self._args(tool_call)
        from core.harness.registry.tool_operations import prepare_tool_arguments
        args = prepare_tool_arguments(tool_name, args, str(getattr(agent.state, "user_id", "") or ""),
                                      str(getattr(agent.state, "chat_id", "") or ""))
        live_chunks: asyncio.Queue = asyncio.Queue()

        async def _invoke():
            from core.harness.usage import (
                UsageAttempt,
                attempt_status_for_exception,
                record_usage_safely,
            )
            from core.harness.tool_effect_context import CURRENT_TOOL_EFFECT

            final = None
            started = time.monotonic()
            usage_status = "failed"
            binding_token = CURRENT_RUN_BINDING.set((run_id, run_owner))
            invoke_kwargs = input_kwargs
            effect = CURRENT_TOOL_EFFECT.get()
            if effect is not None and tool_name in {
                "create_scheduled_task",
                "update_scheduled_task",
                "delete_scheduled_task",
            }:
                adapter_args = dict(args)
                adapter_args["tool_effect_id"] = effect.effect_id
                adapter_call = tool_call.model_copy(
                    update={"input": json.dumps(adapter_args, ensure_ascii=False)}
                )
                invoke_kwargs = {**input_kwargs, "tool_call": adapter_call}
            try:
                outcome_unknown = False
                async for item in next_handler(**invoke_kwargs):
                    # Toolkit normally converts adapter exceptions into ordinary
                    # error responses. An ambiguous remote write must retain its
                    # ledger meaning across that conversion and stop this run.
                    ambiguous = bool(
                        (getattr(item, "metadata", None) or {}).get("gateway_outcome_unknown")
                    )
                    outcome_unknown = outcome_unknown or ambiguous
                    if isinstance(item, ToolResponse):
                        final = item
                    elif not ambiguous:
                        await live_chunks.put(item)
                if outcome_unknown:
                    raise RuntimeError("cloud write outcome unknown; reconciliation required")
                usage_status = (
                    "success"
                    if final is not None and final.state == ToolResultState.SUCCESS
                    else "failed"
                )
            except asyncio.CancelledError:
                usage_status = "cancelled"
                raise
            except Exception as exc:
                usage_status = attempt_status_for_exception(exc)
                raise
            finally:
                CURRENT_RUN_BINDING.reset(binding_token)
                if effect is not None:
                    try:
                        await record_usage_safely(
                            self._usage_ledger,
                            UsageAttempt(
                                run_id=run_id,
                                kind="tool",
                                operation_name=tool_name,
                                effect_id=effect.effect_id,
                                status=usage_status,
                                latency_ms=int((time.monotonic() - started) * 1_000),
                                metadata={"tool_call_id": tool_call_id},
                            ),
                        )
                    except Exception:  # usage cannot change tool execution
                        logger.debug("tool usage attempt persistence failed", exc_info=True)
            if final is None:
                raise self._gateway.response_error(f"tool {tool_name} ended without ToolResponse")
            return self._response_payload(final)

        def _response_failed(payload: Any) -> bool:
            raw = payload.get("tool_response") if isinstance(payload, dict) else None
            return bool(isinstance(raw, dict) and str(raw.get("state") or "success") != "success")

        task = asyncio.create_task(
            self._gateway.execute_outcome(
                run_id=run_id,
                owner=run_owner,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=args,
                invoke=_invoke,
                classify_failure=_response_failed,
            )
        )
        try:
            while not task.done() or not live_chunks.empty():
                try:
                    item = await asyncio.wait_for(live_chunks.get(), timeout=0.02)
                except asyncio.TimeoutError:
                    continue
                yield item
            outcome = await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task
            elif not task.cancelled():
                task.exception()
        response = self._response_from_payload(outcome.result)
        if outcome.result_state == "error" and response.state == ToolResultState.SUCCESS:
            response.state = ToolResultState.ERROR
        linkage = {
            "effect_id": outcome.effect_id,
            "result_id": outcome.result_id,
        }
        response.metadata = {**dict(response.metadata or {}), **linkage}
        links = getattr(agent.state, "tool_effect_links", None)
        if isinstance(links, dict):
            links[tool_call_id] = linkage
        if not outcome.invoked:
            yield ToolChunk(
                content=list(response.content),
                state=response.state,
                metadata=dict(response.metadata),
            )
        yield response
