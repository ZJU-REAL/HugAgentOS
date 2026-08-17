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
from contextvars import ContextVar
from pathlib import Path
from typing import Any, List, Optional

from agentscope.agent import Agent
from agentscope.event import ToolCallEndEvent
from agentscope.message import Base64Source, DataBlock, Msg, TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool._response import ToolChunk, ToolResponse
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
from pydantic import ConfigDict, Field

logger = logging.getLogger(__name__)


def _cyfunc_probe() -> None:  # once compiled by Cython, its type is cython_function_or_method
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
CURRENT_TOOL_CALL_ID: ContextVar[str] = ContextVar("jx_current_tool_call_id", default="")


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
                text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
                if text_blocks and len(text_blocks) == len(blocks):
                    # 发号器绑在 agent 上（run 入口注入）；缺失时就地建并绑定，
                    # 保证编号在该 agent 的整条流里唯一
                    allocator = resolve_allocator(agent)
                    full_text = "".join((b.text or "") for b in text_blocks)
                    new_text, items = annotate_tool_result(tool_name, tool_id, full_text, allocator)
                    if items:
                        allocator.register(tool_id, items)
                        final.content = [TextBlock(type="text", text=new_text)]
                        yield ToolChunk(
                            content=[TextBlock(type="text", text=new_text)],
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
    enable_thinking: bool = True
    chat_mode: str | None = None
    uploaded_files: List[dict] = Field(default_factory=list)
    historical_files: List[dict] = Field(default_factory=list)
    user_message_text: str = ""
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

        notice = "用户追加了新指令；本工具调用已在执行前中止，等待模型按新指令重新规划。"
        block = TextBlock(type="text", text=notice)
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
                agent.state.context.append(
                    Msg(
                        name="user",
                        role="user",
                        content=[
                            TextBlock(
                                type="text",
                                text=(
                                    "[用户在当前执行中追加的新指令]\n"
                                    f"{message}\n"
                                    + (
                                        "请立即按这条新指令调整后续计划；不要继续已经被中止的旧工具调用。"
                                        if interrupted_tools
                                        else "请立即按这条新指令调整后续计划；上一轮工具结果已经完成，可按需使用。"
                                    )
                                ),
                            )
                        ],
                    )
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
            tool_input = raw_input if isinstance(raw_input, dict) else json.loads(raw_input or "{}")
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
                agent.state.context.append(
                    Msg(
                        name="user",
                        role="user",
                        content=[
                            TextBlock(
                                type="text",
                                text=(
                                    "<system-reminder>\n"
                                    "检测到受领域本体治理的运行时资产，策略已升级且本轮不可降级。\n"
                                    f"{contract}\n"
                                    "</system-reminder>"
                                ),
                            )
                        ],
                    )
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
                guidance.append("本体门禁已触发熔断；停止调用该工具，并向用户说明缺失条件。")
            await self._audit(
                agent,
                runtime,
                tool_name,
                decision,
                started,
                denial_count=denial_count,
                circuit_breaker=denial_count >= breaker_threshold,
            )
            if denial_count == strategy_threshold and getattr(agent.state, "chat_id", None):
                try:
                    from core.services.ontology_evolution_service import schedule_ontology_evolution

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
                content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
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
                or (decision.matched_rule_ids[0] if decision.matched_rule_ids else None),
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
    def _resolve_invoked_asset(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
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
            self._inject(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[file_context] failed: %s", exc, exc_info=True)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _inject(self, agent: Agent) -> None:
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
                st.context.append(
                    Msg(
                        name="user",
                        role="user",
                        content=[TextBlock(type="text", text=hist_context)],
                    )
                )

        # 1. Current-turn text files
        text_context = (
            _build_file_context(uploaded_files, user_id=user_id) if uploaded_files else ""
        )
        if text_context:
            st.context.append(
                Msg(name="user", role="user", content=[TextBlock(type="text", text=text_context)])
            )

        # 2. Images: 2.0 merges DataBlock(Base64Source) into the last user message
        image_files = [f for f in uploaded_files if _is_image(f)]
        if not image_files:
            return
        image_blocks: list = []
        image_names: list[str] = []
        for f in image_files:
            result = _fetch_image_base64(f, user_id=user_id)
            if result:
                b64_data, mime_type = result
                image_blocks.append(
                    DataBlock(
                        type="data",
                        source=Base64Source(type="base64", media_type=mime_type, data=b64_data),
                    )
                )
                image_names.append(f.get("name", "图片"))
        if not image_blocks:
            return
        names_str = "、".join(image_names)
        prefix_block = TextBlock(
            type="text", text=f"[用户上传了 {len(image_blocks)} 张图片：{names_str}]"
        )
        # Find the last user message to merge into (user messages allow text + data blocks)
        last_user_msg = None
        for i in range(len(st.context) - 1, -1, -1):
            if getattr(st.context[i], "role", None) == "user":
                last_user_msg = st.context[i]
                break
        if last_user_msg is not None:
            merged = [prefix_block, *image_blocks, *(last_user_msg.content or [])]
            last_user_msg.content = merged
        else:
            st.context.append(Msg(name="user", role="user", content=[prefix_block, *image_blocks]))


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
        # Append the system-reminder directly to context (visible in the next reasoning round; verified in spike #2)
        agent.state.context.append(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        type="text", text=f"<system-reminder>\n{reminder}\n</system-reminder>"
                    )
                ],
            )
        )
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
        agent.state.context.append(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        type="text", text=f"<system-reminder>\n{reminder}\n</system-reminder>"
                    )
                ],
            )
        )
        self._since_last = 0
        if output_hit:
            self._output_seen = True


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

    def _maybe_force_text(self, input_kwargs: dict, max_iters: int, remaining: int) -> dict:
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

    def _maybe_remind(self, agent: Agent, max_iters: int, cur_iter: int, remaining: int) -> None:
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
        agent.state.context.append(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        type="text", text=f"<system-reminder>\n{reminder}\n</system-reminder>"
                    )
                ],
            )
        )


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
        key = (getattr(agent.state, "reply_id", "") or "", int(getattr(agent.state, "cur_iter", 0) or 0))
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
        agent.state.context.append(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(type="text", text=f"<system-reminder>\n{reminder}\n</system-reminder>")
                ],
            )
        )


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
                json.dumps(getattr(tool_call, "input", None), sort_keys=True, default=str),
            )
        except Exception:  # noqa: BLE001
            signature = (tool_name, "")

        if signature == self._last_call:
            self._signals["repeated_actions"] = self._signals.get("repeated_actions", 0) + 1
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
            self._signals["tool_error_streak"] = self._signals.get("tool_error_streak", 0) + 1
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

        agent.state.context.append(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"<system-reminder>\n{observed}。{guidance}\n"
                            f"（该干预来自当前生效的编排配置：{signal} ≥ "
                            f"{getattr(rule, 'threshold', 0)} → {action}）\n</system-reminder>"
                        ),
                    )
                ],
            )
        )
        logger.info(
            "[stall-intervention] %s >= %s -> %s", signal, getattr(rule, "threshold", 0), action
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
                        "[finish_guard] auto-pinned %d/%d file_id(s)", pinned_now, len(unpinned)
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[finish_guard] failed: %s", exc)


__all__ = [
    "AgentRuntimeState",
    "SteerMiddleware",
    "DynamicModelMiddleware",
    "FileContextMiddleware",
    "WorkspacePinHintMiddleware",
    "GoalAnchorReminderMiddleware",
    "IterBudgetReminderMiddleware",
    "FinishPinGuardMiddleware",
]
