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

import base64
import logging
from contextvars import ContextVar
from typing import Any, List

from pydantic import ConfigDict, Field

from agentscope.agent import Agent
from agentscope.event import ToolCallEndEvent
from agentscope.message import DataBlock, Base64Source, Msg, TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState

from core.llm.hooks import (
    _GOAL_ANCHOR_INTERVAL,
    _GOAL_ANCHOR_OUTPUT_TOOLS,
    _GOAL_ANCHOR_REMINDER_TEMPLATE,
    _GOAL_ANCHOR_WARMUP_CALLS,
    _FILE_ID_RE,
    _PIN_HINT_SKIP_TOOLS,
    _build_file_context,
    _build_historical_files_context,
    _fetch_image_base64,
    _get_main_model,
    _get_provider_model,
    _get_pin_hint_state,
    _is_image,
    _resolve_chat_mode,
    reset_artifact_read_state,
    reset_pin_hint_state,
)

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
    enable_thinking: bool = True
    chat_mode: str | None = None
    uploaded_files: List[dict] = Field(default_factory=list)
    historical_files: List[dict] = Field(default_factory=list)
    user_message_text: str = ""

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
        self.enable_thinking = bool(context.get("enable_thinking", True))
        cm = str(context.get("chat_mode") or "").lower() or None
        if cm:
            self.chat_mode = cm
        self.uploaded_files = list(context.get("uploaded_files", []) or [])
        self.historical_files = list(context.get("historical_files", []) or [])
        self.user_message_text = user_message_text or ""


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

    async def on_reasoning(self, agent: Agent, input_kwargs: dict, next_handler):
        try:
            self._maybe_remind(agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[iter_budget] failed: %s", exc)
        async for evt in next_handler(**input_kwargs):
            yield evt

    def _maybe_remind(self, agent: Agent) -> None:
        max_iters = int(getattr(agent.react_config, "max_iters", 0) or 0)
        if max_iters <= self._threshold + 1:
            return
        cur_iter = int(getattr(agent.state, "cur_iter", 0) or 0)
        remaining = max_iters - cur_iter  # includes the current round
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
    "DynamicModelMiddleware",
    "FileContextMiddleware",
    "WorkspacePinHintMiddleware",
    "GoalAnchorReminderMiddleware",
    "IterBudgetReminderMiddleware",
    "FinishPinGuardMiddleware",
]
