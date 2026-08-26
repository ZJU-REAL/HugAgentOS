"""AgentScope 2.0 middlewares — replace the 1.x hooks (pre_reply / post_acting / post_reasoning).

1.x → 2.0 migration mapping:
  - dynamic_model (pre_reply)        → DynamicModelMiddleware.on_reply
  - file_context  (pre_reply)        → FileContextMiddleware.on_reply
  - workspace_pin_hint (post_acting) → WorkspacePinHintMiddleware.on_reasoning
        (⚠️ on_acting cannot write to context; must use on_reasoning to inject the reminder before the next reasoning round)
  - goal_anchor (post_acting)        → GoalAnchorReminderMiddleware.on_reasoning
  - finish_pin_guard (post_reasoning)→ FinishPinGuardMiddleware.on_reasoning
  - iter_budget (new in 2.0, no 1.x predecessor) → IterBudgetReminderMiddleware.on_reasoning

The runtime context moved from ``agent._jx_context`` to ``agent.state`` (fields on an AgentRuntimeState subclass).
Pure-logic helpers still reuse hooks.py / finish_guard.py (they are agentscope-version independent).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, List, Optional

from agentscope.agent import Agent
from agentscope.event import ToolCallEndEvent
from agentscope.message import ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool._response import ToolChunk, ToolResponse
from core.llm.context_adapter import (
    append_context_text,
    append_state_context_item,
    next_context_sequence,
    render_text_block,
)
from core.llm.context_ir import (
    KIND_ATTACHMENT,
    KIND_PROJECT,
    KIND_REMINDER,
    KIND_STEER,
    POLICY_HEAD_TAIL,
    POLICY_NEVER,
    ContextItem,
)
from core.llm.hooks import (
    _FILE_ID_RE,
    _GOAL_ANCHOR_INTERVAL,
    _GOAL_ANCHOR_OUTPUT_TOOLS,
    _GOAL_ANCHOR_REMINDER_TEMPLATE,
    _GOAL_ANCHOR_WARMUP_CALLS,
    _PIN_HINT_SKIP_TOOLS,
    _build_file_context,
    _build_historical_files_context,
    _fetch_image_base64,
    _get_main_model,
    _get_pin_hint_state,
    _get_provider_model,
    _is_image,
    _resolve_chat_mode,
    reset_artifact_read_state,
    reset_pin_hint_state,
)
from core.llm.plan_update_tool import parse_plan_update_args
from pydantic import ConfigDict, Field

logger = logging.getLogger(__name__)


def _append_reminder(agent: Agent, text: str, *, origin: str) -> None:
    append_context_text(
        agent,
        f"<system-reminder>\n{text}\n</system-reminder>",
        kind=KIND_REMINDER,
        origin=origin,
        trust="system",
        priority=850,
        token_budget=4_000,
        truncation_policy=POLICY_HEAD_TAIL,
    )


def _append_state_payload(
    state: AgentState,
    content: Any,
    *,
    kind: str,
    origin: str,
    trust: str,
    priority: int = 650,
    token_budget: int = 8_000,
) -> None:
    created_seq = next_context_sequence(state.context)
    append_state_context_item(
        state,
        ContextItem.create(
            item_id=f"{origin}:{created_seq}",
            kind=kind,
            origin=origin,
            trust=trust,
            visibility="model",
            priority=priority,
            token_budget=token_budget,
            truncation_policy=POLICY_HEAD_TAIL,
            content=content,
            cache_class="dynamic",
            created_seq=created_seq,
            render_role="user",
            message_group=f"{origin}:{created_seq}",
        ),
    )


def _cyfunc_probe() -> (
    None
):  # once compiled by Cython, its type is cython_function_or_method
    pass


# Cython compiles methods into cython_function_or_method; pydantic v2 doesn't recognize it as a
# method and treats it as an "unannotated field", raising PydanticUserError — so after hardened
# compilation the whole module fails to import and falls back to plaintext. Registering that type
# in ignored_types makes pydantic ignore the compiled methods. Under pure Python it is just a
# regular FunctionType (pydantic already ignores methods), so there is no side effect.
_CYFUNCTION_TYPE = type(_cyfunc_probe)


# ── Current tool-call id seam ───────────────────────────────────────────────
# When a tool function needs to know its own tool_call_id (e.g. call_subagent attaches the
# subagent's streaming events to its own card via parent_tool_id), AgentScope does not pass it
# down (toolkit.call_tool does not inject the id into tool kwargs). Use an on_acting middleware
# to write the current tool_call.id into a ContextVar before each tool executes — the tool runs
# in the **same task call chain**, so it can read it.
# Concurrent tool calls each run in separate tasks spawned by asyncio.gather (each with its own
# copy of the context), so ContextVars don't cross-contaminate — naturally aligned with parallel subagents.
CURRENT_TOOL_CALL_ID: ContextVar[str] = ContextVar(
    "jx_current_tool_call_id", default=""
)
CURRENT_RUN_BINDING: ContextVar[tuple[str, str] | None] = ContextVar(
    "jx_current_run_binding", default=None
)


class ActingToolCallIdMiddleware(MiddlewareBase):
    """on_acting: expose the current tool_call.id to tool functions (see above). Pure pass-through, does not change tool behavior."""

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        tc = input_kwargs.get("tool_call")
        tcid = getattr(tc, "id", "") or "" if tc is not None else ""
        token = CURRENT_TOOL_CALL_ID.set(tcid) if tcid else None
        try:
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            if token is not None:
                try:
                    CURRENT_TOOL_CALL_ID.reset(token)
                except Exception:  # noqa: BLE001
                    pass


class ToolEffectMiddleware(MiddlewareBase):
    """Commit durable Intent before every harness-owned tool invocation.

    The final ``ToolResponse`` is committed before it leaves this middleware,
    so the existing AgentScope event/SSE mapping remains unchanged while the
    database becomes authoritative for replay and reconciliation.
    """

    def __init__(self, session_factory=None, registry=None) -> None:  # noqa: ANN001
        from core.db.engine import SessionLocal
        from core.services.tool_effect_ledger import (
            DEFAULT_TOOL_RECOVERY_REGISTRY,
            ToolEffectGateway,
            ToolEffectJournal,
        )

        self._ledger = ToolEffectJournal(session_factory or SessionLocal)
        self._registry = registry or DEFAULT_TOOL_RECOVERY_REGISTRY
        self._gateway = ToolEffectGateway(self._ledger, self._registry)
        from core.services.harness_ledger import HarnessUsageLedger

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
                content=[
                    render_text_block(
                        json.dumps(payload, ensure_ascii=False, default=str)
                    )
                ],
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

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        from core.services.tool_effect_ledger import ToolIntentCommitError

        run_id = str(getattr(agent.state, "run_id", "") or "")
        run_owner = str(getattr(agent.state, "journal_owner", "") or "")
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if not run_id or not run_owner or not tool_name:
            raise ToolIntentCommitError(
                "tool execution requires a durable run_id and journal_owner binding"
            )

        args = self._args(tool_call)
        live_chunks: asyncio.Queue = asyncio.Queue()

        async def _invoke():
            from core.services.tool_effect_ledger import CURRENT_TOOL_EFFECT
            from core.harness.usage import (
                UsageAttempt,
                attempt_status_for_exception,
                record_usage_safely,
            )

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
                async for item in next_handler(**invoke_kwargs):
                    if isinstance(item, ToolResponse):
                        final = item
                    else:
                        await live_chunks.put(item)
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
                        logger.debug(
                            "tool usage attempt persistence failed", exc_info=True
                        )
            if final is None:
                from core.services.tool_effect_ledger import ToolEffectError

                raise ToolEffectError(f"tool {tool_name} ended without ToolResponse")
            return self._response_payload(final)

        def _response_failed(payload: Any) -> bool:
            raw = payload.get("tool_response") if isinstance(payload, dict) else None
            return bool(
                isinstance(raw, dict)
                and str(raw.get("state") or "success") != "success"
            )

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
        if (
            outcome.result_state == "error"
            and response.state == ToolResultState.SUCCESS
        ):
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


class CitationAnchorMiddleware(MiddlewareBase):
    """on_acting: 统一证据锚点——工具结果回给模型前完成 提取 → 发号 → cite_id 回注。

    协议依据（agentscope 2.0 toolkit.call_tool）：中间 ToolChunk 是增量、最后一个
    yield 是**累积完整**的 ToolResponse；SSE 侧（orchestration/streaming.py）只在
    ToolResultEndEvent 时把 delta 累积成一条 tool_result，不实时消费中间增量。
    因此这里缓冲中间块，在最终 ToolResponse 上注号后：
      1) 合成一个携带完整注号文本的 ToolChunk（SSE 累积到的就是注号后文本）；
      2) 用注号文本替换 ToolResponse.content（模型上下文 / 落库拿到同一份）。
    两侧看到的 cite_id 因此严格一致。跳过名单、非纯文本内容、非 SUCCESS 状态、
    任何异常 → 原样放行（引用功能降级，绝不影响工具本身）。
    """

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        from orchestration.citation_anchor import (
            SKIP_TOOLS,
            annotate_tool_result,
            resolve_allocator,
        )

        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        tool_id = str(getattr(tool_call, "id", "") or "")
        if not tool_name or tool_name in SKIP_TOOLS:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        buffered: list = []
        final: ToolResponse | None = None
        async for item in next_handler(**input_kwargs):
            if isinstance(item, ToolResponse):
                final = item
                break  # per call_tool protocol the ToolResponse is the last yield
            buffered.append(item)

        if final is None:
            for item in buffered:
                yield item
            return

        annotated = False
        if final.state == ToolResultState.SUCCESS:
            try:
                blocks = list(final.content or [])
                text_blocks = [b for b in blocks if getattr(b, "type", "") == "text"]
                if text_blocks and len(text_blocks) == len(blocks):
                    # 发号器绑在 agent 上（run 入口注入）；缺失时就地建并绑定，
                    # 保证编号在该 agent 的整条流里唯一
                    allocator = resolve_allocator(agent)
                    full_text = "".join((b.text or "") for b in text_blocks)
                    new_text, items = annotate_tool_result(
                        tool_name, tool_id, full_text, allocator
                    )
                    if items:
                        allocator.register(tool_id, items)
                        final.content = [render_text_block(new_text)]
                        yield ToolChunk(
                            content=[render_text_block(new_text)],
                            state=final.state,
                            metadata=dict(final.metadata or {}),
                        )
                        annotated = True
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[citation-anchor] middleware annotate failed tool=%s",
                    tool_name,
                    exc_info=True,
                )

        if not annotated:
            for item in buffered:
                yield item
        yield final


class AgentRuntimeState(AgentState):
    """Extends AgentState to carry the runtime fields of the former ModelContext (replaces agent._jx_context)."""

    model_config = ConfigDict(ignored_types=(_CYFUNCTION_TYPE,))

    model_name: str = ""
    model_provider_id: str = ""
    # When a subagent explicitly configures a model (model_provider_id) → the factory sets this
    # True, and DynamicModel must not override it with the main model based on chat_mode
    # (otherwise the subagent's own model is effectively useless — neither channels nor web get it).
    model_pinned: bool = False
    user_id: str | None = None
    chat_id: str | None = None
    run_id: str | None = None
    journal_owner: str | None = None
    tool_effect_links: dict = Field(default_factory=dict)
    enable_thinking: bool = True
    chat_mode: str | None = None
    uploaded_files: List[dict] = Field(default_factory=list)
    historical_files: List[dict] = Field(default_factory=list)
    user_message_text: str = ""
    # Vision evidence transcribed ahead of the model call by the streaming entry
    # point; FileContextMiddleware injects it verbatim instead of redoing the
    # network round-trip. Empty = transcribe inline (non-streaming callers).
    vision_evidence_text: str = ""
    ontology_enabled: bool = False
    ontology_runtime: dict = Field(default_factory=dict)
    # Set by SteerMiddleware immediately before the next reasoning round.  The
    # streaming adapter turns it into one ``steer_applied`` event, then clears
    # it. Keeping this on the typed runtime state avoids process-global queues.
    steer_delivery: dict | None = None

    def apply_request_context(self, context: dict, user_message_text: str) -> None:
        """Populate per-request runtime fields from the request ``context`` dict (replaces the 1.x agent._jx_context).

        Shared by both entry points — streaming and workflow (non-streaming) — to keep two
        hand-copied field mappings from silently drifting (streaming once didn't lowercase
        chat_mode while workflow did).
        """
        self.model_name = str(context.get("model_name", "") or "")
        self.model_provider_id = str(context.get("model_provider_id", "") or "")
        self.user_id = str(context.get("user_id", "") or "") or None
        self.chat_id = str(context.get("chat_id", "") or "") or None
        self.run_id = str(context.get("run_id", "") or "") or None
        self.journal_owner = str(context.get("journal_owner", "") or "") or None
        self.enable_thinking = bool(context.get("enable_thinking", True))
        cm = str(context.get("chat_mode") or "").lower() or None
        if cm:
            self.chat_mode = cm
        self.uploaded_files = list(context.get("uploaded_files", []) or [])
        self.historical_files = list(context.get("historical_files", []) or [])
        self.user_message_text = user_message_text or ""
        self.ontology_enabled = bool(context.get("ontology_enabled", False))
        runtime = context.get("ontology_runtime")
        self.ontology_runtime = runtime if isinstance(runtime, dict) else {}


class ExplicitConnectorInvocationError(RuntimeError):
    """The selected connector could not satisfy its mandatory real-call contract."""


class ExplicitConnectorToolChoiceMiddleware(MiddlewareBase):
    """Force and verify a real tool call for a user-selected connector.

    The reasoning hook sends only the selected connector's tool schemas with
    ``tool_choice=required`` until one of those tools actually passes through
    the acting hook.  The reply hook is the fail-closed boundary: a provider
    that ignores ``tool_choice`` cannot silently return a normal answer.
    """

    def __init__(self, *, connector_ids: List[str], tool_names: List[str]) -> None:
        self.connector_ids = tuple(dict.fromkeys(str(x) for x in connector_ids if x))
        self.tool_names = tuple(dict.fromkeys(str(x) for x in tool_names if x))
        if not self.connector_ids or not self.tool_names:
            raise ValueError("connector_ids and tool_names must not be empty")
        self._satisfied = False
        self._force_logged = False

    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler):
        self._satisfied = False
        self._force_logged = False
        completed = False
        try:
            async for evt in next_handler(**input_kwargs):
                yield evt
            completed = True
        finally:
            if not completed:
                self._force_logged = False
        if not self._satisfied:
            connector_text = ", ".join(self.connector_ids)
            raise ExplicitConnectorInvocationError(
                "显式选择的连接器未完成真实工具调用"
                f"（{connector_text}）；本轮已停止，避免在未调用连接器的情况下生成回答。"
            )

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        if not self._satisfied:
            from agentscope.tool import ToolChoice

            input_kwargs = {
                **input_kwargs,
                "tool_choice": ToolChoice(
                    mode="required",
                    tools=list(self.tool_names),
                ),
            }
            if not self._force_logged:
                logger.info(
                    "[connector-required] forcing connector_ids=%s tools=%s",
                    list(self.connector_ids),
                    list(self.tool_names),
                )
                self._force_logged = True
        async for evt in next_handler(**input_kwargs):
            yield evt

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        is_selected_connector_tool = tool_name in self.tool_names
        completed = False
        async for evt in next_handler(**input_kwargs):
            yield evt
        completed = True
        if is_selected_connector_tool and completed:
            self._satisfied = True
            logger.info(
                "[connector-required] real connector tool call completed: %s",
                tool_name,
            )


class SteerMiddleware(MiddlewareBase):
    """Deliver a queued user instruction at the next safe ReAct boundary.

    ``on_reasoning`` is the primary insertion point: AgentScope calls it after
    the previous tool results have entered context and immediately before the
    next model call. ``on_acting`` is the earlier fallback for a steer that
    arrives while the model is constructing its next tool call; it marks that
    not-yet-started tool batch interrupted so the next reasoning round can
    replan. Both paths preserve a valid assistant-tool-result-user order.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._delivery: dict | None = None
        self._interrupted_tools = False

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        run_id = str(getattr(agent.state, "run_id", "") or "")
        if not run_id:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        async with self._lock:
            if self._delivery is None:
                from core.services.chat_steer_service import take_pending_steer

                self._delivery = await take_pending_steer(run_id)
                self._interrupted_tools = self._delivery is not None
            delivery = self._delivery

        if delivery is None:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        notice = (
            "用户追加了新指令；本工具调用已在执行前中止，等待模型按新指令重新规划。"
        )
        block = render_text_block(notice)
        yield ToolChunk(content=[block], state=ToolResultState.INTERRUPTED)
        yield ToolResponse(content=[block], state=ToolResultState.INTERRUPTED)

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        run_id = str(getattr(agent.state, "run_id", "") or "")
        async with self._lock:
            # A steer can arrive while a long-running tool is executing. There
            # is no later on_acting hook in that round, so poll again here,
            # after AgentScope saved the tool result and before it starts the
            # next model call. This is the normal "insert after this tool"
            # path; without it the steer waits until another tool call (or the
            # whole run) finishes.
            if self._delivery is None and run_id:
                from core.services.chat_steer_service import take_pending_steer

                self._delivery = await take_pending_steer(run_id)
            delivery = self._delivery
            interrupted_tools = self._interrupted_tools
            self._delivery = None
            self._interrupted_tools = False

        if delivery is not None:
            message = str(delivery.get("message") or "").strip()
            if message:
                append_context_text(
                    agent,
                    "[用户在当前执行中追加的新指令]\n"
                    f"{message}\n"
                    + (
                        "请立即按这条新指令调整后续计划；不要继续已经被中止的旧工具调用。"
                        if interrupted_tools
                        else "请立即按这条新指令调整后续计划；上一轮工具结果已经完成，可按需使用。"
                    ),
                    kind=KIND_STEER,
                    origin="user:steer",
                    trust="user",
                    priority=950,
                    token_budget=4_000,
                    truncation_policy=POLICY_NEVER,
                )
                agent.state.steer_delivery = dict(delivery)

        async for item in next_handler(**input_kwargs):
            yield item


class OntologyGateMiddleware(MiddlewareBase):
    """L-a deterministic gate: validate every visible tool call without an LLM."""

    def __init__(self, runtime: dict | None = None) -> None:
        self.runtime = runtime if isinstance(runtime, dict) else {}
        self.completed_tools: set[str] = set()
        self.denial_counts: dict[str, int] = {}

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        state_runtime = getattr(agent.state, "ontology_runtime", None)
        runtime = state_runtime if isinstance(state_runtime, dict) else self.runtime
        if not runtime.get("enabled"):
            async for item in next_handler(**input_kwargs):
                yield item
            return

        from core.ontology.validator import (
            activate_runtime_for_asset,
            evaluate_tool_call,
            render_runtime_prompt,
        )
        from core.services.ontology_service import resolve_runtime_asset_tags

        started = time.monotonic()
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        raw_input = getattr(tool_call, "input", "{}")
        try:
            tool_input = (
                raw_input
                if isinstance(raw_input, dict)
                else json.loads(raw_input or "{}")
            )
        except (TypeError, json.JSONDecodeError):
            tool_input = {}
        asset_kind, asset_id = self._resolve_invoked_asset(tool_name, tool_input)
        await asyncio.to_thread(
            resolve_runtime_asset_tags,
            runtime=runtime,
            kind=asset_kind,
            asset_id=asset_id,
            user_id=str(getattr(agent.state, "user_id", "") or ""),
        )
        activations = activate_runtime_for_asset(
            runtime,
            kind=asset_kind,
            asset_id=asset_id,
        )
        if activations:
            agent.state.ontology_enabled = True
            agent.state.ontology_runtime = runtime
            contract = render_runtime_prompt(runtime)
            if contract:
                _append_reminder(
                    agent,
                    "检测到受领域本体治理的运行时资产，策略已升级且本轮不可降级。\n"
                    f"{contract}",
                    origin="harness:ontology_gate",
                )
            await self._audit_activations(agent, runtime, activations)
        decision = evaluate_tool_call(
            runtime,
            tool_name=tool_name,
            tool_input=tool_input,
            completed_tools=self.completed_tools,
        )
        if not decision.allowed:
            key = ",".join(decision.matched_rule_ids) or tool_name
            self.denial_counts[key] = self.denial_counts.get(key, 0) + 1
            denial_count = self.denial_counts[key]
            thresholds = [pack.get("config", {}) for pack in runtime.get("packs", [])]
            strategy_threshold = min(
                (int(item.get("repeated_denial_threshold", 2)) for item in thresholds),
                default=2,
            )
            breaker_threshold = min(
                (int(item.get("circuit_breaker_threshold", 5)) for item in thresholds),
                default=5,
            )
            guidance = list(decision.suggestions)
            if denial_count >= strategy_threshold:
                guidance.append("同一规则已重复拦截，请改变执行策略，不要原样重试。")
            if denial_count >= breaker_threshold:
                guidance.append(
                    "本体门禁已触发熔断；停止调用该工具，并向用户说明缺失条件。"
                )
            await self._audit(
                agent,
                runtime,
                tool_name,
                decision,
                started,
                denial_count=denial_count,
                circuit_breaker=denial_count >= breaker_threshold,
            )
            if denial_count == strategy_threshold and getattr(
                agent.state, "chat_id", None
            ):
                try:
                    from core.services.ontology_evolution_service import (
                        schedule_ontology_evolution,
                    )

                    schedule_ontology_evolution(
                        user_id=str(getattr(agent.state, "user_id", "") or "system")
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ontology] evolution scheduling failed: %s", exc)
            payload = {
                "code": "ONTOLOGY_GATE_DENIED",
                "tool": tool_name,
                "violations": decision.violations,
                "suggestions": guidance,
                "denial_count": denial_count,
                "circuit_breaker": denial_count >= breaker_threshold,
            }
            yield ToolChunk(
                content=[render_text_block(json.dumps(payload, ensure_ascii=False))],
                state=ToolResultState.DENIED,
                metadata={"ontology_gate": payload},
            )
            return

        await self._audit(agent, runtime, tool_name, decision, started)

        last_state = None
        async for item in next_handler(**input_kwargs):
            last_state = getattr(item, "state", last_state)
            yield item
        if last_state not in {
            ToolResultState.ERROR,
            ToolResultState.DENIED,
            ToolResultState.INTERRUPTED,
        }:
            self.completed_tools.add(tool_name)

    async def _audit(
        self,
        agent: Agent,
        runtime: dict[str, Any],
        tool_name: str,
        decision,
        started: float,
        *,
        denial_count: int = 0,
        circuit_breaker: bool = False,
    ) -> None:  # noqa: ANN001
        if not decision.violations and not decision.matched_rule_ids:
            return
        runtime.setdefault("runtime_events", []).append(
            {
                "type": "ontology_gate",
                "status": "completed",
                "governance_run_id": runtime.get("governance_run_id"),
                "stage": "tool",
                "decision": decision.decision,
                "tool_name": tool_name,
                "matched_rule_ids": list(decision.matched_rule_ids),
                "violations": list(decision.violations),
                "suggestions": list(decision.suggestions),
                "denial_count": denial_count,
                "circuit_breaker": circuit_breaker,
            }
        )
        try:
            from core.services.ontology_service import record_enforcement_event

            first = decision.violations[0] if decision.violations else {}
            pack_id = first.get("pack_id")
            version_id = None
            for pack in runtime.get("packs", []):
                if pack.get("pack_id") == pack_id:
                    version_id = pack.get("version_id")
                    break
            payload = {
                "user_id": getattr(agent.state, "user_id", None),
                "chat_id": getattr(agent.state, "chat_id", None),
                "pack_id": pack_id,
                "version_id": version_id,
                "rule_id": first.get("rule_id")
                or (
                    decision.matched_rule_ids[0] if decision.matched_rule_ids else None
                ),
                "stage": "tool",
                "event_type": "tool_call_gate",
                "decision": decision.decision,
                "mode": first.get("mode", "enforce"),
                "target": tool_name,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "details": {
                    "governance_run_id": runtime.get("governance_run_id"),
                    "violations": decision.violations,
                    "matched_rule_ids": decision.matched_rule_ids,
                    "suggestions": decision.suggestions,
                    "denial_count": denial_count,
                    "circuit_breaker": circuit_breaker,
                },
            }
            await asyncio.to_thread(record_enforcement_event, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology] audit event persistence failed: %s", exc)

    @staticmethod
    def _resolve_invoked_asset(
        tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[str, str]:
        if tool_name == "call_subagent" and tool_input.get("agent_id"):
            return "subagent", str(tool_input["agent_id"])
        if tool_name == "view_text_file":
            file_path = str(tool_input.get("file_path") or "")
            path = Path(file_path)
            if path.name == "SKILL.md" and path.parent.name:
                return "skill", path.parent.name
        return "tool", tool_name

    @staticmethod
    async def _audit_activations(
        agent: Agent,
        runtime: dict[str, Any],
        activations: list[dict[str, Any]],
    ) -> None:
        try:
            from core.services.ontology_service import record_runtime_activation

            for event in activations:
                await asyncio.to_thread(
                    record_runtime_activation,
                    event,
                    runtime,
                    user_id=getattr(agent.state, "user_id", None),
                    chat_id=getattr(agent.state, "chat_id", None),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology] activation audit persistence failed: %s", exc)


# ── DynamicModel ──────────────────────────────────────────────────────────
class DynamicModelMiddleware(MiddlewareBase):
    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            # The subagent pinned its own model → skip the chat_mode-based main-model override
            # and respect its configuration. Otherwise the subagent model built by the factory
            # from model_provider_id would be unconditionally replaced with the main model here —
            # symptom: "subagent channel binding doesn't take effect / web and channel behave differently".
            if not getattr(agent.state, "model_pinned", False):
                mode = _resolve_chat_mode(agent.state)
                provider_id = getattr(agent.state, "model_provider_id", "") or ""
                if provider_id:
                    agent.model = _get_provider_model(provider_id, mode=mode)
                else:
                    agent.model = _get_main_model(mode=mode)
                run_id = str(getattr(agent.state, "run_id", "") or "")
                if run_id:
                    from core.llm.model_usage import instrument_model_usage

                    instrument_model_usage(agent.model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dynamic_model] failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt


# ── FileContext ───────────────────────────────────────────────────────────
class FileContextMiddleware(MiddlewareBase):
    def __init__(self) -> None:
        self._injected = False

    async def on_reply(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            await self._inject(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[file_context] failed: %s", exc, exc_info=True)
        async for evt in next_handler(**input_kwargs):
            yield evt

    async def _inject(self, agent: Agent) -> None:
        # Reset per-turn state at each reply-turn boundary (read_artifact budget / pin-reminder bookkeeping)
        reset_artifact_read_state()
        reset_pin_hint_state()

        st = agent.state
        uploaded_files = list(getattr(st, "uploaded_files", None) or [])
        historical_files = list(getattr(st, "historical_files", None) or [])
        if not uploaded_files and not historical_files:
            return
        # user_id serves as the ownership-verification gate for attachment reads (prevents forged
        # file_id cross-user reads; see the _download_artifact_bytes / _build_file_context docstrings in hooks).
        user_id = getattr(st, "user_id", None) or None
        if self._injected:
            return
        self._injected = True

        # 0. Historical files digest
        if historical_files:
            hist_context = _build_historical_files_context(historical_files)
            if hist_context:
                _append_state_payload(
                    st,
                    hist_context,
                    kind=KIND_PROJECT,
                    origin="workspace:historical_files",
                    trust="workspace",
                )

        # 1. Current-turn text files.
        #    Off the event loop: on a parse-cache miss _build_file_context downloads the
        #    artifact and POSTs it to the external file-parser service with blocking
        #    ``requests`` — measured at 79s for an 8MB PDF, and the service timeout allows
        #    120s. Called inline it pinned the whole loop for that long: every SSE stream
        #    (all chats, all users) emitted nothing, queued requests all landed in the same
        #    millisecond once it returned, and even /health stalled. The visible symptom is
        #    "streaming stopped working" on the very turn a document is attached.
        text_context = (
            await asyncio.to_thread(
                _build_file_context, uploaded_files, user_id=user_id
            )
            if uploaded_files
            else ""
        )
        if text_context:
            _append_state_payload(
                st,
                text_context,
                kind=KIND_ATTACHMENT,
                origin="user:uploaded_files",
                trust="user",
            )

        # 2. Images. Two paths, decided by whether the *effective* model can see:
        #    - natively multimodal  → pass the bytes through as DataBlocks (best fidelity)
        #    - text-only           → vision bridge transcribes them into text evidence
        #      (core/vision), because the media blocks would otherwise be dropped
        #      downstream in chat_models._without_multimodal_content and the model
        #      would be blind to the upload.
        image_files = [f for f in uploaded_files if _is_image(f)]
        if not image_files:
            return
        if _effective_model_supports_vision(st):
            # Same reason: each image is downloaded from storage (S3/OSS in deployments
            # that use them) and base64-encoded — blocking I/O plus CPU on the loop.
            await asyncio.to_thread(
                self._inject_native_images, st, image_files, user_id
            )
        else:
            await self._inject_vision_evidence(st, image_files, user_id)

    # ── image injection paths ──────────────────────────────────────────────

    @staticmethod
    def _inject_native_images(st: AgentState, image_files: list, user_id) -> None:
        """Natively multimodal model: append framework-neutral image blocks."""
        image_blocks: list = []
        image_names: list[str] = []
        for f in image_files:
            result = _fetch_image_base64(f, user_id=user_id)
            if result:
                b64_data, mime_type = result
                image_blocks.append(
                    {
                        "type": "data",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64_data,
                        },
                    }
                )
                image_names.append(f.get("name", "图片"))
        if not image_blocks:
            return
        names_str = "、".join(image_names)
        prefix_block = {
            "type": "text",
            "text": f"[用户上传了 {len(image_blocks)} 张图片：{names_str}]",
        }
        _append_state_payload(
            st,
            [prefix_block, *image_blocks],
            kind=KIND_ATTACHMENT,
            origin="user:uploaded_images",
            trust="user",
            token_budget=16_000,
        )

    @staticmethod
    async def _inject_vision_evidence(
        st: AgentState, image_files: list, user_id
    ) -> None:
        """Text-only model: inject the images as text evidence from the vision bridge.

        The streaming entry point usually transcribes ahead of time (so it can report
        "reading image" progress while the network call is in flight) and leaves the
        result on ``st.vision_evidence_text``. When that is absent — the non-streaming
        workflow, channel bots — this transcribes inline as before.
        """
        precomputed = (getattr(st, "vision_evidence_text", "") or "").strip()
        if precomputed:
            _append_state_payload(
                st,
                precomputed,
                kind=KIND_ATTACHMENT,
                origin="vision:transcription",
                trust="tool",
            )
            return

        from core.vision.attachments import transcribe_attachments

        result = await transcribe_attachments(image_files, user_id=user_id)
        if result is None or not result.text:
            return
        _append_state_payload(
            st,
            result.text,
            kind=KIND_ATTACHMENT,
            origin="vision:transcription",
            trust="tool",
        )


def _effective_model_supports_vision(st: AgentState) -> bool:
    """Whether the model this turn will actually run on can read images natively.

    Mirrors DynamicModelMiddleware's resolution order (per-request provider override
    first, then the main_agent role), so the decision matches the model that ends up
    receiving the request.
    """
    try:
        from core.services.model_config import ModelConfigService
        from core.vision import model_supports_vision

        service = ModelConfigService.get_instance()
        provider_id = (getattr(st, "model_provider_id", "") or "").strip()
        cfg = service.resolve_provider(provider_id) if provider_id else None
        return model_supports_vision(cfg or service.resolve("main_agent"))
    except Exception as exc:  # noqa: BLE001 — unknown capability degrades to the bridge
        logger.warning("[vision] capability probe failed, assuming text-only: %s", exc)
        return False


# ── WorkspacePinHint ───────────────────────────────────────────────────────
def _recent_tool_results(context: list, scan: int = 12) -> list:
    """Return the most recent tool_result blocks at the tail of context (2.0: pydantic blocks on assistant messages)."""
    blocks = []
    for msg in context[-scan:]:
        try:
            if msg.has_content_blocks("tool_result"):
                blocks.extend(msg.get_content_blocks("tool_result"))
        except Exception:  # noqa: BLE001
            continue
    return blocks


def _tool_result_text(block: Any) -> str:
    from core.llm.message_compat import flatten_tool_output

    return flatten_tool_output(getattr(block, "output", None))


class WorkspacePinHintMiddleware(MiddlewareBase):
    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            self._scan_and_remind(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pin_hint] failed: %s", exc, exc_info=True)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _scan_and_remind(self, agent: Agent) -> None:
        from core.llm import workspace as _ws

        results = _recent_tool_results(agent.state.context)
        if not results:
            return
        state = _get_pin_hint_state()
        seen: set = state["seen"]
        for block in results:
            name = getattr(block, "name", "") or ""
            if name in _PIN_HINT_SKIP_TOOLS:
                continue
            text_blob = _tool_result_text(block)
            if '"file_id"' in text_blob:
                seen.update(_FILE_ID_RE.findall(text_blob))
        if not seen:
            return
        pinned = set(_ws.get_pinned_file_ids())
        unpinned = seen - pinned
        if not unpinned:
            return
        sig = ",".join(sorted(unpinned))
        if sig == state.get("last_reminded_sig"):
            return
        preview = sorted(unpinned)[:6]
        preview_str = ", ".join(preview)
        if len(unpinned) > len(preview):
            preview_str += f", …(+{len(unpinned) - len(preview)} 个)"
        reminder = (
            f"沙盒里有 {len(unpinned)} 个 file_id 还没 pin：[{preview_str}]。"
            f"若是给用户的最终产物，必须调 `pin_to_workspace(file_ids=[...])` 才能交付。"
        )
        _append_reminder(agent, reminder, origin="harness:workspace_pin")
        state["last_reminded_sig"] = sig


# ── GoalAnchorReminder ─────────────────────────────────────────────────────
class GoalAnchorReminderMiddleware(MiddlewareBase):
    def __init__(self, *, chat_id: str | None = None, batch_mode: bool = False) -> None:
        self._chat_id = chat_id
        self._batch_mode = batch_mode
        self._count = 0
        self._since_last = 0
        self._output_seen = False

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        if not self._batch_mode:
            try:
                self._maybe_remind(agent)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[goal_anchor] failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _maybe_remind(self, agent: Agent) -> None:
        original = (getattr(agent.state, "user_message_text", "") or "").strip()
        if not original:
            return
        self._count += 1
        self._since_last += 1
        if self._count < _GOAL_ANCHOR_WARMUP_CALLS:
            return
        # Detect whether an output tool call appeared recently
        output_hit = False
        if not self._output_seen:
            for msg in agent.state.context[-6:]:
                try:
                    for b in msg.get_content_blocks("tool_call"):
                        if getattr(b, "name", "") in _GOAL_ANCHOR_OUTPUT_TOOLS:
                            output_hit = True
                            break
                except Exception:  # noqa: BLE001
                    continue
        interval_hit = self._since_last >= _GOAL_ANCHOR_INTERVAL
        if not (interval_hit or output_hit):
            return
        reminder = _GOAL_ANCHOR_REMINDER_TEMPLATE.format(original=original)
        _append_reminder(agent, reminder, origin="harness:goal_anchor")
        self._since_last = 0
        if output_hit:
            self._output_seen = True


# ── PlanStaleReminder ──────────────────────────────────────────────────────
# 停滞满 N 轮催一次，催完重新计时——等价于 Claude Code 那两个都取 10 的阈值。
_PLAN_REMINDER_INTERVAL = 10

_PLAN_STALE_REMINDER_TEMPLATE = """`update_plan` 已经 {rounds} 轮没有用过了。当前计划栏停在 {done}/{total}：
{checklist}

如果其中有步骤其实已经做完，可以调用一次 `update_plan` 把状态补上（传全量列表）；如果这份
清单已经不符合你正在做的事，重写它比让它停在原地更好。仅在确实相关时才使用——这只是一条
温和的提醒，不适用就忽略，不要为了回应它而打断手上的工作。不要向用户提起这条提醒。"""

def _block_attr(block: Any, name: str, default: Any = None) -> Any:
    """内容块在 AS2 里是 pydantic 对象，但历史路径上也出现过 dict —— 两种都读。"""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _latest_plan_call(context: list) -> tuple[Optional[dict], str]:
    """倒着找最近一次 ``update_plan`` 调用，返回（解析后的计划, 这次调用的指纹）。

    指纹用 tool_call 的 id：id 变了就说明模型刚更新过清单，计时要清零。拿不到 id 时
    退化成清单内容本身——同样能区分"又调了一次且内容有变"，只在"重复提交完全相同的
    清单"时无法分辨，而那种情况本来也不该重置计时。
    """
    for msg in reversed(list(context or [])):
        try:
            if hasattr(msg, "has_content_blocks") and not msg.has_content_blocks("tool_call"):
                continue
            blocks = list(msg.get_content_blocks("tool_call") or [])
        except Exception:  # noqa: BLE001
            continue
        for b in reversed(blocks):
            if _block_attr(b, "name", "") != "update_plan":
                continue
            raw = _block_attr(b, "input", None)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    raw = None
            plan = parse_plan_update_args(raw)
            fingerprint = str(_block_attr(b, "id", "") or "") or json.dumps(
                plan or {}, ensure_ascii=False, sort_keys=True
            )
            return plan, fingerprint
    return None, ""


class PlanStaleReminderMiddleware(MiddlewareBase):
    """计划栏停滞满 ``_PLAN_REMINDER_INTERVAL`` 轮时，把当前清单软提醒给模型一次。

    模型没建过清单、或清单已全部结算，都不打扰；模型自己更新了清单则重新计时。
    """

    def __init__(self, *, interval: int = _PLAN_REMINDER_INTERVAL) -> None:
        self._interval = max(1, int(interval))
        self._since_last = 0
        self._last_fingerprint: Optional[str] = None

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            self._maybe_remind(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[plan_stale] failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _maybe_remind(self, agent: Agent) -> None:
        plan, fingerprint = _latest_plan_call(agent.state.context)
        if not plan:
            return
        if fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self._since_last = 0
            return
        steps = plan.get("steps") or []
        if not any(s.get("status") in ("pending", "in_progress") for s in steps):
            return
        self._since_last += 1
        if self._since_last < self._interval:
            return
        done = sum(1 for s in steps if s.get("status") == "completed")
        checklist = "\n".join(
            f"  {i}. [{s.get('status', 'pending')}] {s.get('title', '')}"
            for i, s in enumerate(steps, start=1)
        )
        _append_reminder(
            agent,
            _PLAN_STALE_REMINDER_TEMPLATE.format(
                done=done,
                total=len(steps),
                rounds=self._since_last,
                checklist=checklist,
            ),
            origin="harness:plan_stale",
        )
        logger.info(
            "[plan_stale] reminded at %d/%d after %d idle rounds",
            done,
            len(steps),
            self._since_last,
        )
        self._since_last = 0


# ── IterBudgetReminder ─────────────────────────────────────────────────────
class IterBudgetReminderMiddleware(MiddlewareBase):
    """Inject a wrap-up reminder as ReAct approaches max_iters, so the model soft-lands instead of erroring on the hard limit.

    When AgentScope 2.0 runs max_iters dry it only yields ExceedMaxItersEvent + one fixed
    English error string, and everything produced in the turn is discarded (in the subagent
    scenario the error string is returned to the main agent as the tool result). This
    middleware appends a system-reminder to context when remaining rounds <= threshold,
    with two levels of wording:
      - N (>1) rounds left: stop exploring/retrying, consolidate the information gathered so far and wrap up;
      - final round: no further tool calls allowed — the framework loop gives no more reasoning
        chances before exiting, so a tool call in the final round is guaranteed to trip the hard
        limit (_agent.py main-loop semantics).

    Dedup key is (reply_id, cur_iter): remind only once per round; across rounds the wording
    escalates in urgency as remaining rounds decrease. When max_iters is very small
    (<= threshold+1, e.g. plan wrap-up style micro budgets), don't remind — avoid nagging to
    wrap up right after starting.
    """

    def __init__(self, *, threshold: int = 2) -> None:
        self._threshold = threshold
        self._last_key: tuple | None = None
        # Env is immutable for the process lifetime — parse the kill-switch once
        # per middleware (one per agent build), not on every reasoning round.
        from core.config.settings import _bool, _env

        self._force_text = _bool(_env("CHAT_FINAL_ITER_FORCE_TEXT", "true"))

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        max_iters, cur_iter, remaining = self._budget(agent)
        try:
            self._maybe_remind(agent, max_iters, cur_iter, remaining)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[iter_budget] failed: %s", exc)
        try:
            input_kwargs = self._maybe_force_text(input_kwargs, max_iters, remaining)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[iter_budget] force-text failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt

    @staticmethod
    def _budget(agent: Agent) -> tuple[int, int, int]:
        """Return (max_iters, cur_iter, remaining-including-current-round)."""
        max_iters = int(getattr(agent.react_config, "max_iters", 0) or 0)
        cur_iter = int(getattr(agent.state, "cur_iter", 0) or 0)
        return max_iters, cur_iter, max_iters - cur_iter

    def _maybe_force_text(
        self, input_kwargs: dict, max_iters: int, remaining: int
    ) -> dict:
        """Final round: force tool_choice="none" so the model can only produce text.

        The reminder alone is advisory — a model that still emits a tool call in
        the final round trips AgentScope's hard limit and the whole turn's output
        is discarded. Forcing text turns the hard error into a guaranteed clean
        exit (the loop sees no tool calls and yields the reply Msg). Skipped for
        micro budgets (same guard as the reminder) and when a caller already
        pinned an explicit tool_choice. Kill-switch: CHAT_FINAL_ITER_FORCE_TEXT=false.
        """
        if remaining > 1 or max_iters <= self._threshold + 1 or not self._force_text:
            return input_kwargs
        if input_kwargs.get("tool_choice") is not None:
            return input_kwargs
        from agentscope.tool import ToolChoice

        logger.info(
            "[iter_budget] final round (max_iters=%d): forcing tool_choice=none for clean delivery",
            max_iters,
        )
        return {**input_kwargs, "tool_choice": ToolChoice(mode="none")}

    def _maybe_remind(
        self, agent: Agent, max_iters: int, cur_iter: int, remaining: int
    ) -> None:
        if max_iters <= self._threshold + 1:
            return
        if remaining > self._threshold:
            return
        key = (getattr(agent.state, "reply_id", "") or "", cur_iter)
        if key == self._last_key:
            return
        self._last_key = key
        if remaining <= 1:
            reminder = (
                "这是本次回复的最后一轮推理：不要再调用任何工具（再调用会被系统强制"
                "中断，已完成的工作将无法交付）。请直接基于已获得的信息输出最终回复；"
                "若任务未全部完成，如实汇报已产出的成果、当前进展和未完成的部分。"
            )
        else:
            reminder = (
                f"注意：推理-工具调用轮次即将用尽，包含本轮在内最多还剩 {remaining} 轮。"
                "请停止新的探索，不要再重试已反复失败的操作，立即整合已获得的信息进行"
                "收尾；若任务无法在剩余轮次内全部完成，优先输出已有成果与进展说明。"
            )
        _append_reminder(agent, reminder, origin="harness:iteration_budget")


# ── JobLedgerReminder ──────────────────────────────────────────────────────
class JobLedgerReminderMiddleware(MiddlewareBase):
    """本会话存在未收敛的批量作业时，每轮把台账状态回灌进上下文。

    为什么要 harness 主动回灌，而不是指望模型记着：进度是**外部事实**，写在
    ``job_items`` 表里，不在模型的记忆里。模型隔了十几轮之后对"还剩多少没做"的印象
    只会越来越糊，最后就演变成"边际收益递减，先交付吧"——上一轮 568 行只补 66 行
    就是这么停的。

    每轮注入一条 ``<system-reminder>``，内容是可查证的数字（总计/已完成/待办/失败）
    和明确的下一步。同一轮只注入一次；台账已收敛（待办与失败都为 0）就不再打扰。
    """

    def __init__(self, *, chat_id: Optional[str], user_id: Optional[str]) -> None:
        self._chat_id = chat_id or ""
        self._user_id = user_id or ""
        self._last_key: tuple | None = None

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            self._maybe_remind(agent)
        except Exception as exc:  # noqa: BLE001 —— 提醒失败绝不该拖垮主链路
            logger.warning("[job_ledger] reminder failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _maybe_remind(self, agent: Agent) -> None:
        if not self._chat_id:
            return
        key = (
            getattr(agent.state, "reply_id", "") or "",
            int(getattr(agent.state, "cur_iter", 0) or 0),
        )
        if key == self._last_key:
            return

        from core.db.engine import SessionLocal
        from core.db.models import Job
        from core.services.job_service import JobService

        with SessionLocal() as db:
            jobs = (
                db.query(Job)
                .filter(Job.chat_id == self._chat_id, Job.user_id == self._user_id)
                .order_by(Job.created_at.desc())
                .limit(3)
                .all()
            )
            if not jobs:
                return
            svc = JobService(db)
            lines = []
            for job in jobs:
                stats = svc.stats(job.job_id)
                unsettled = int(stats.get("pending", 0)) + int(stats.get("failed", 0))
                if unsettled <= 0 and job.status in ("completed", "cancelled"):
                    continue
                lines.append(
                    f"- 作业「{job.name or job.job_id}」（{job.job_id}，状态 {job.status}）："
                    f"总计 {stats.get('total', 0)}，已完成 {stats.get('done', 0)}，"
                    f"查无 {stats.get('not_found', 0)}，待办 {stats.get('pending', 0)}，"
                    f"失败 {stats.get('failed', 0)}"
                )
        if not lines:
            return

        self._last_key = key
        reminder = (
            "本会话有尚未结算完的批量作业（数字取自台账，是可查证的事实，不是估计）：\n"
            + "\n".join(lines)
            + "\n\n还有待办或失败项时：用 run_job(action='resume', job_id=...) 续跑"
            "（已完成的项不会重做），或先查 run_job(action='status', job_id=...) 看明细。"
            "**不得**把未完成当作完成来交付；确实要收尾，也必须如实报出分母、完成数与未覆盖清单。"
        )
        _append_reminder(agent, reminder, origin="harness:job_ledger")


# ── StallIntervention ──────────────────────────────────────────────────────
class StallInterventionMiddleware(MiddlewareBase):
    """Apply the active profile's intervention rules to the **ReAct loop**.

    The rules existed before this and only ever reached ``autonomous_loop`` — the
    long-running self-driving product — through
    :mod:`core.evolution.policies`. That left the orchestration profile with one
    field that governed nothing on the axis nearly all traffic takes, and it is a
    fair reading of "you are still tuning a workflow": every other part of the
    profile shapes the ReAct assembly, this one did not.

    What a ReAct turn can actually observe is narrower than what a multi-run loop
    can, and pretending otherwise is how a rule ends up never firing:

    * ``repeated_actions`` — the same tool called with the same arguments again;
    * ``tool_error_streak`` — consecutive failing tool calls;
    * ``no_progress`` — consecutive calls that returned nothing usable.

    ``no_diff`` and ``reviewer_score_flat`` need a file tree and a reviewer
    verdict, which a single answer has neither of; those stay loop-only and are
    declared as such in :mod:`core.evolution.agent_profile`.

    The intervention itself is a ``<system-reminder>`` appended to the context —
    the same mechanism the iteration-budget reminder uses. A middleware cannot
    reach into the model's plan, and it should not: the model is told what has
    been observed and what to do about it, and stays responsible for the how.
    """

    def __init__(self, rules: List[Any] | None = None) -> None:
        self._rules = list(rules or [])
        self._signals: dict[str, int] = {}
        self._last_call: tuple | None = None
        self._fired: set[str] = set()

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        if not self._rules:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        try:
            signature = (
                tool_name,
                json.dumps(
                    getattr(tool_call, "input", None), sort_keys=True, default=str
                ),
            )
        except Exception:  # noqa: BLE001
            signature = (tool_name, "")

        if signature == self._last_call:
            self._signals["repeated_actions"] = (
                self._signals.get("repeated_actions", 0) + 1
            )
        else:
            # A streak is consecutive by definition; a different call breaks it.
            self._signals["repeated_actions"] = 0
        self._last_call = signature

        last_state = None
        async for item in next_handler(**input_kwargs):
            last_state = getattr(item, "state", last_state)
            yield item

        failed = last_state in {
            ToolResultState.ERROR,
            ToolResultState.DENIED,
            ToolResultState.INTERRUPTED,
        }
        if failed:
            self._signals["tool_error_streak"] = (
                self._signals.get("tool_error_streak", 0) + 1
            )
            self._signals["no_progress"] = self._signals.get("no_progress", 0) + 1
        else:
            self._signals["tool_error_streak"] = 0
            self._signals["no_progress"] = 0

        try:
            self._maybe_intervene(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[stall-intervention] failed: %s", exc)

    def _maybe_intervene(self, agent: Agent) -> None:
        from core.evolution.agent_profile import (
            ACTION_CHANGE_STRATEGY,
            ACTION_DELEGATE,
            ACTION_ESCALATE,
            ACTION_NARROW_SCOPE,
            ACTION_RETRY,
            ACTION_ROLLBACK_AND_FORK,
            ACTION_STOP,
        )

        applicable = [
            rule
            for rule in self._rules
            if self._signals.get(getattr(rule, "signal", ""), 0)
            >= int(getattr(rule, "threshold", 0) or 0)
            and int(getattr(rule, "threshold", 0) or 0) > 0
        ]
        if not applicable:
            return
        # Most-specific first: a long stall escalates rather than repeating the
        # mild response that already failed to help.
        rule = max(applicable, key=lambda r: int(getattr(r, "threshold", 0) or 0))
        action = str(getattr(rule, "action", ""))
        signal = str(getattr(rule, "signal", ""))

        # Fire once per (signal, action). Repeating the same reminder every
        # iteration turns it into noise the model learns to skip.
        key = f"{signal}:{action}"
        if key in self._fired:
            return
        self._fired.add(key)

        observed = {
            "repeated_actions": "你已经用完全相同的参数重复调用同一个工具",
            "tool_error_streak": "连续多次工具调用失败",
            "no_progress": "连续多轮没有取得任何可用结果",
        }.get(signal, "当前执行已停滞")

        guidance = {
            ACTION_CHANGE_STRATEGY: "换一条思路：不要重试同样的调用，改用别的工具或别的切入角度。",
            ACTION_NARROW_SCOPE: "缩小范围：先只解决其中最小的一个子问题，拿到确定结果再往下走。",
            ACTION_RETRY: "可以再试一次，但必须改变参数或前置条件，原样重试不会有不同结果。",
            ACTION_DELEGATE: "把这段工作交给合适的子智能体处理，只取回结论。",
            ACTION_ESCALATE: "停止尝试，向用户说明卡在哪里、还缺什么信息。",
            ACTION_STOP: "停止继续尝试，如实汇报已完成的部分和未完成的原因。",
            # Loop-only: a single answer has nothing to roll back to.
            ACTION_ROLLBACK_AND_FORK: "放弃当前路径，回到上一个可用状态重新规划。",
        }.get(action, "")
        if not guidance:
            return

        _append_reminder(
            agent,
            f"{observed}。{guidance}\n"
            f"（该干预来自当前生效的编排配置：{signal} ≥ "
            f"{getattr(rule, 'threshold', 0)} → {action}）",
            origin="harness:stall_intervention",
        )
        logger.info(
            "[stall-intervention] %s >= %s -> %s",
            signal,
            getattr(rule, "threshold", 0),
            action,
        )


# ── FinishPinGuard ─────────────────────────────────────────────────────────
class FinishPinGuardMiddleware(MiddlewareBase):
    def __init__(self, *, batch_mode: bool = False) -> None:
        self._batch_mode = batch_mode
        self._fired = False

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        had_tool_call = False
        async for evt in next_handler(**input_kwargs):
            if isinstance(evt, ToolCallEndEvent):
                had_tool_call = True
            yield evt
        if self._batch_mode or had_tool_call or self._fired:
            return
        try:
            from core.llm.finish_guard import _collect_unpinned, _direct_pin

            unpinned = _collect_unpinned()
            if unpinned:
                pinned_now = _direct_pin(unpinned)
                if pinned_now > 0:
                    self._fired = True
                    logger.info(
                        "[finish_guard] auto-pinned %d/%d file_id(s)",
                        pinned_now,
                        len(unpinned),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[finish_guard] failed: %s", exc)


__all__ = [
    "AgentRuntimeState",
    "ExplicitConnectorInvocationError",
    "ExplicitConnectorToolChoiceMiddleware",
    "SteerMiddleware",
    "DynamicModelMiddleware",
    "FileContextMiddleware",
    "WorkspacePinHintMiddleware",
    "GoalAnchorReminderMiddleware",
    "IterBudgetReminderMiddleware",
    "FinishPinGuardMiddleware",
]
