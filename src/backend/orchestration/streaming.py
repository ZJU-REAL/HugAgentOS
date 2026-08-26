"""Streaming agent wrapper for AgentScope 2.0.

Consumes ``agent.reply_stream(...)`` (replaces the 1.x msg_queue) and maps the 25
fine-grained EventType into the internal events consumed by ``workflow.py``:
- ("text_delta", str)      - incremental answer text
- ("thinking_delta", str)  - incremental reasoning
- ("tool_call_start", dict)- tool call started (args still streaming)
- ("tool_call_delta", dict)- incremental tool-call argument JSON
- ("tool_call", dict)      - tool invocation complete
- ("tool_result", dict)    - tool invocation result
- ("file_confirm", dict)   - myspace write confirmation (in-house ContextVar gate, distinct from native HITL)
- ("user_question", dict)  - model question requesting resident-composer input
- ("user_question_resolved", dict) - server-authoritative answer/cancel/timeout
- ("heartbeat", None)      - silence heartbeat (queue empty ≥3s: the model produced nothing at all)
- ("model_progress", None) - throttled liveness signal, emitted in two situations. (a) upstream events
                             keep arriving but none maps to an SSE event (throttled tool-call args /
                             suppressed thinking streaming); (b) a model call is in flight (ModelCallStart seen,
                             no end yet) and the queue is silent — a hung/slow LLM endpoint produces
                             zero events per attempt, and without this signal the run inactivity
                             watchdog killed healthy-but-waiting runs as "卡死" instead of letting the
                             HTTP read timeout surface the real model error. Unlike "heartbeat" it
                             counts as activity for the run inactivity watchdog; zombie protection is
                             delegated to the httpx timeouts on the model client while in flight.
- ("error", Exception|dict)- agent error (dict shaped like ExceedMaxIters' {kind,name})

No standalone end event: ``stream()`` ends the generator directly upon the internal done
sentinel (consumers terminate naturally via ``async for``). Model-produced DataBlocks
(image_chunk etc.) are currently not forwarded — no downstream consumer, and the configured
models only take images as input; see the fall-through at the end of ``_map_event``.

Migration notes (1.x → 2.0):
- ``agent.msg_queue`` / ``set_msg_queue_enabled`` removed → ``reply_stream``.
- Events are inherently incremental, no accumulate→delta conversion needed (but some models,
  e.g. deepseekv4-flash, inline the chain of thought as ``<think>...</think>`` in text deltas,
  which still needs suppression when enable_thinking=False).
- usage is accumulated from ``ModelCallEndEvent`` (the 1.x _UsageTrackingModel proxy is no longer needed).
- ctx is set on ``agent.state`` (AgentRuntimeState) instead of ``agent._jx_context``.
- The myspace HITL gate still has to drain concurrently with event consumption → reply_stream
  runs in a background task feeding the queue.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from agentscope.agent import Agent
from agentscope.mcp import MCPClient
from agentscope.message import Msg
from core.infra.logging import LogContext
from core.llm.context_adapter import (
    next_request_sequence,
    render_context_item,
    render_session_input,
)
from core.llm.context_ir import (
    KIND_USER_INPUT,
    POLICY_NEVER,
    SESSION_CONTEXT_META_KEY,
    ContextItem,
)
from core.llm._distill_shared import _THINK_BLOCK_RE
from core.llm.message_compat import session_to_msgs
from core.services import log_service as log_writer

logger = logging.getLogger(__name__)


_HTTP_ERR_RE = re.compile(r"HTTP\s*[45]\d\d")

# Minimum spacing of ("model_progress", None) liveness signals. During long
# tool-call-argument generation the model streams ToolCallDeltaEvent for
# minutes while _map_event yields nothing downstream — without this signal the
# run inactivity watchdog (CHAT_RUN_INACTIVITY_TIMEOUT_SEC) sees pure silence
# and kills a healthy run mid-generation.
_MODEL_PROGRESS_MIN_INTERVAL_S = 5.0

# Queue poll interval of the stream() main loop — on each timeout it emits a
# heartbeat (or model_progress while a model call is in flight). Module-level for
# testability.
_QUEUE_POLL_INTERVAL_S = 3.0

# Tool-call argument deltas can arrive one or two characters at a time. Sending
# every upstream fragment recreates the historical SSE storm (hundreds of
# events for a single heredoc), while swallowing all fragments hides a model
# capability the UI can render. Flush when either threshold is crossed to
# preserve visible streaming without forwarding every tiny fragment.
_TOOL_CALL_DELTA_FLUSH_INTERVAL_S = 0.05
_TOOL_CALL_DELTA_FLUSH_CHARS = 256


def _looks_like_tool_error(content: str) -> bool:
    if not content:
        return False
    s = content.strip()
    if not s:
        return False
    head = s[:64]
    if s.startswith(('{"error"', "{'error'", '{"ok": false', '{"ok":false')):
        return True
    if '"ok": false' in head or '"ok":false' in head:
        return True
    if s.startswith(
        ("Error executing tool", "Error: ", "Traceback (most recent call last)")
    ):
        return True
    if "validation error for" in head:
        return True
    if "不存在或已删除" in s or "无权访问" in head:
        return True
    if _HTTP_ERR_RE.search(s):
        return True
    return False


def _strip_thinking_answer(
    raw: str, enable_thinking: bool, in_thinking: bool
) -> Tuple[str, bool]:
    """Extract the user-visible "answer" portion from the accumulated raw text.

    Returns (answer, new_in_thinking). When enable_thinking=True, returns as-is (the frontend
    parses <think> itself); otherwise suppresses every <think>…</think> span and returns only
    the visible text.

    一次模型调用里可以出现**多段**思考链（ReAct 模型常在工具调用前再想一轮）。
    早先的实现只取"最后一个 </think> 之后的内容"：当第二段思考刚开了 <think>
    还没闭合时，rfind 命中的是第一段的闭标签，于是 ``vis1<think>正在生成的思考…``
    被整段当成正文吐出去 —— 网页端 enable_thinking=False 只在快速模式出现（模型
    本来就不思考），而定时任务这条路径恰恰是"模型照常思考、正文里不该有思考"，
    于是思考过程就跟着落进了自动化任务的输出正文。

    改成"先删掉所有闭合块，再处理孤儿标签"，未闭合的那段思考不会再漏出来。
    """
    if enable_thinking:
        return raw, False
    # 1) 闭合的思考块整段丢掉（可能有多段）
    text = _THINK_BLOCK_RE.sub("", raw)
    # 2) 孤儿闭标签：有些服务端把开标签吃进 chat template，正文以裸思考开头，
    #    最后一个 </think> 之前的都是思考。
    last_end = text.rfind("</think>")
    if last_end != -1:
        text = text[last_end + len("</think>") :]
        return text, False
    # 3) 还没闭合的思考块：它之后的内容都还在思考里，不出正文
    open_idx = text.find("<think>")
    if open_idx != -1:
        return text[:open_idx], True
    # 4) 整段没有任何标签：沿用上一次的判定（服务端预填开标签的形态下，
    #    在 </think> 到来之前只能靠这一位把裸思考压住）
    if in_thinking:
        return "", True
    return text, False


class StreamingAgent:
    """Wraps an AgentScope 2.0 ``Agent`` to produce streaming SSE events via reply_stream."""

    def __init__(
        self,
        agent: Agent,
        mcp_clients: List[MCPClient],
    ):
        self.agent = agent
        self.mcp_clients = mcp_clients
        self._enable_thinking = False
        # usage: accumulated from ModelCallEndEvent
        self._usage_records: List[Dict[str, int]] = []
        # tool_id → {tool_name, tool_args(str), started_monotonic, started_at}
        self._pending_tool_calls: Dict[str, Dict[str, Any]] = {}
        # tool_id → name (recorded at ToolCallStart), tool_id → accumulated args string (ToolCallDelta)
        self._tool_name_buf: Dict[str, str] = {}
        self._tool_args_buf: Dict[str, str] = {}
        # tool_id → not-yet-emitted argument delta / last flush timestamp
        self._tool_delta_emit_buf: Dict[str, str] = {}
        self._tool_delta_last_emit: Dict[str, float] = {}
        # tool_id → accumulated result text (ToolResultTextDelta)
        self._tool_result_buf: Dict[str, str] = {}
        # Accumulated answer text (for dedup in the <think>-suppression scenario)
        self._raw_text = ""
        self._emitted_answer = ""
        # 首轮正文累计（enable_thinking 下）：轮次结束时若整轮没有 </think>，
        # 说明该模型不是"内联思考"形态（结构化 reasoning 通道、或本轮无思考）——
        # 补发 structured_reasoning 标记，让前端把误缓冲成"思考"的正文流回正文区。
        self._first_round_text = ""
        self._round_index = 0
        self._structured_marker_sent = False
        self._in_thinking = False
        self._reasoning_protocol_emitted = False
        # True between ModelCallStartEvent and ModelCallEndEvent. While a call is
        # in flight the LLM endpoint may legitimately produce nothing for minutes
        # (long prefill on a huge prompt / hung gateway being retried); the queue
        # poll then emits model_progress instead of heartbeat so the run
        # inactivity watchdog doesn't kill a run that is merely waiting on the
        # model — the model client's own httpx timeouts remain the backstop.
        self._model_call_inflight = False

    def _take_reasoning_protocol(self) -> Optional[Dict[str, bool]]:
        """Return the structured-reasoning marker once the active model is known.

        DynamicModelMiddleware may replace ``agent.model`` at reply start, so this is
        evaluated immediately before mapping the first AgentScope event rather than in
        ``__init__``.
        """
        if self._reasoning_protocol_emitted:
            return None
        model = getattr(self.agent, "model", None)
        if not bool(getattr(model, "structured_reasoning", False)):
            return None
        self._reasoning_protocol_emitted = True
        return {"structured_reasoning": True}

    def get_usage(self) -> Dict[str, int]:
        """Return the in-memory fallback; async production paths use aget_usage."""
        total_prompt = sum(r.get("prompt_tokens", 0) for r in self._usage_records)
        total_completion = sum(
            r.get("completion_tokens", 0) for r in self._usage_records
        )
        last = self._usage_records[-1] if self._usage_records else {}
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "llm_call_count": len(self._usage_records),
            "context_tokens": int(last.get("prompt_tokens", 0) or 0)
            + int(last.get("completion_tokens", 0) or 0),
        }

    async def aget_usage(self) -> Dict[str, int]:
        """Derive usage from append-only attempts without blocking the event loop."""
        run_id = str(getattr(getattr(self.agent, "state", None), "run_id", "") or "")
        if run_id:
            try:
                from core.services.harness_ledger import HarnessUsageLedger

                attempts = await asyncio.wait_for(
                    asyncio.to_thread(HarnessUsageLedger().attempts, run_id),
                    timeout=1.0,
                )
                model_attempts = tuple(row for row in attempts if row.kind == "model")
                if model_attempts:
                    primary_attempts = tuple(
                        row
                        for row in model_attempts
                        if row.metadata.get("source")
                        not in {"vision", "compaction", "followup"}
                    )
                    last = (primary_attempts or model_attempts)[-1]
                    return {
                        "prompt_tokens": sum(
                            row.usage.prompt_tokens for row in model_attempts
                        ),
                        "completion_tokens": sum(
                            row.usage.completion_tokens for row in model_attempts
                        ),
                        "cache_read_tokens": sum(
                            row.usage.cache_read_tokens for row in model_attempts
                        ),
                        "cache_write_tokens": sum(
                            row.usage.cache_write_tokens for row in model_attempts
                        ),
                        "total_tokens": sum(
                            row.usage.total_tokens for row in model_attempts
                        ),
                        "llm_call_count": len(model_attempts),
                        "context_tokens": last.usage.prompt_tokens
                        + last.usage.completion_tokens,
                    }
            except Exception:
                logger.debug("usage ledger aggregate unavailable", exc_info=True)
        return self.get_usage()

    async def _prepare_vision_evidence(self, st: Any) -> AsyncIterator[Tuple[str, Any]]:
        """Transcribe this turn's image attachments, emitting progress around the wait.

        Yields ``("vision_progress", {...})`` when there is actually something to
        read, so the frontend can label the wait "图像理解中" instead of the generic
        turn spinner. Silent (no events, no delay) when the turn has no images or the
        running model reads images natively — in the native case the picture goes to
        the model as-is and no transcription happens at all.

        Never raises: a failure here degrades to the middleware's inline path.
        """
        try:
            from core.llm.middlewares import _effective_model_supports_vision
            from core.vision.attachments import (
                image_attachments,
                transcribe_attachments,
            )

            images = image_attachments(list(getattr(st, "uploaded_files", None) or []))
            if not images or _effective_model_supports_vision(st):
                return

            yield (
                "vision_progress",
                {
                    "status": "running",
                    "count": len(images),
                    "names": [f.get("name") or "图片" for f in images],
                },
            )
            from core.llm.model_usage import model_usage_scope
            from core.services.harness_ledger import HarnessUsageLedger

            run_id = str(getattr(st, "run_id", "") or "")
            with model_usage_scope(run_id, HarnessUsageLedger()):
                result = await transcribe_attachments(
                    images,
                    user_id=getattr(st, "user_id", None),
                )
            if result is not None and result.text:
                st.vision_evidence_text = result.text
            yield (
                "vision_progress",
                {
                    "status": "done",
                    "count": result.count if result else 0,
                    "ok": bool(result and result.ok),
                    "duration_seconds": round(result.duration_seconds, 2)
                    if result
                    else 0.0,
                },
            )
        except Exception as exc:  # noqa: BLE001 — fall back to inline transcription
            logger.warning(
                "[stream] vision pre-pass failed, deferring to middleware: %s", exc
            )
            yield ("vision_progress", {"status": "done", "count": 0, "ok": False})

    async def stream(
        self,
        session_messages: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> AsyncIterator[Tuple[str, Any]]:
        agent = self.agent

        _last_user_text = ""
        if session_messages:
            for _m in reversed(session_messages):
                if _m.get("role") in ("user", "human"):
                    _last_user_text = str(_m.get("content") or "")
                    break

        # ctx → agent.state (AgentRuntimeState, replaces the 1.x agent._jx_context = ModelContext)
        st = agent.state
        try:
            st.apply_request_context(context, _last_user_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[stream] set agent.state failed: %s", exc)
        self._enable_thinking = bool(context.get("enable_thinking", True))

        # Vision bridge: transcribe uploaded images *before* handing off to the model,
        # so the UI can say "读图中" while it happens. Reading an image is a plain
        # network wait of several seconds; folded inside the agent turn it is
        # indistinguishable from the model thinking, and the user just watches a
        # generic spinner count up. FileContextMiddleware injects the result.
        async for _vision_event in self._prepare_vision_evidence(st):
            yield _vision_event

        _log_ctx = LogContext(user_id=st.user_id or None, chat_id=st.chat_id or None)
        _log_ctx.__enter__()

        # Load history into context (excluding the last user message — reply_stream's inputs carries it, avoiding duplication)
        history = list(session_messages)
        last_user_content = ""
        last_user_row = None
        if history and history[-1].get("role") in ("user", "human"):
            last_user_row = history.pop()
            last_user_content = last_user_row.get("content", "")
        if history:
            try:
                agent.state.context.extend(session_to_msgs(history))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[stream] load history failed: %s", exc)

        user_msg: Optional[Msg] = None
        if last_user_content:
            request_seq = next_request_sequence(agent.state.context)
            if isinstance((last_user_row or {}).get(SESSION_CONTEXT_META_KEY), dict):
                user_msg = render_session_input(last_user_row, created_seq=request_seq)
            else:
                user_msg = render_context_item(
                    ContextItem.create(
                        item_id=f"request:user_input:{request_seq}",
                        kind=KIND_USER_INPUT,
                        origin="user:chat",
                        trust="user",
                        visibility="model",
                        priority=1_000,
                        token_budget=100_000,
                        truncation_policy=POLICY_NEVER,
                        content=last_user_content,
                        cache_class="dynamic",
                        created_seq=request_seq,
                        render_role="user",
                        render_name="user",
                        message_group=f"request:user_input:{request_seq}",
                    )
                )

        # myspace write-confirmation gate (in-house ContextVar, distinct from 2.0 native HITL)
        from core.llm.tools import _myspace_confirm as _mc

        _confirm_chat_id = st.chat_id or None

        def _drain_confirm_signals() -> list:
            out: list = []
            q = _mc.get_ui_queue(_confirm_chat_id)
            if q is None:
                return out
            while True:
                try:
                    out.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            return out

        from core.llm.tools import user_questions as _user_questions

        def _drain_user_question_signals() -> list:
            out: list = []
            q = _user_questions.get_ui_queue(_confirm_chat_id)
            if q is None:
                return out
            while True:
                try:
                    out.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            return out

        def _drain_human_interaction_events() -> list:
            """Map every queued human-interaction signal to a stream event."""

            events: list = []
            for signal in _drain_confirm_signals():
                event_type = (
                    "design_pick"
                    if (signal or {}).get("kind") == _mc.KIND_DESIGN_PICK
                    else "file_confirm"
                )
                events.append((event_type, signal))
            for signal in _drain_user_question_signals():
                signal_event = signal.get("event")
                if signal_event == "requested":
                    events.append(("user_question", signal))
                elif signal_event == "resolved":
                    events.append(("user_question_resolved", signal))
            return events

        # reply_stream runs as a background task feeding the queue; the main loop drains confirm signals concurrently
        event_q: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        # Subagent streaming bypass: register this run's event_q so call_subagent (separate thread)
        # can deliver the subagent's thinking/tool_call/... events back into this main queue in real time.
        from core.llm import _subagent_stream

        _subagent_stream.attach(st.chat_id, asyncio.get_running_loop(), event_q)

        async def _produce():
            try:
                async for ev in agent.reply_stream(inputs=user_msg):
                    await event_q.put(("ev", ev))
            except BaseException as e:  # noqa: BLE001
                import traceback

                logger.error(
                    "Agent reply_stream failed: %r\n%s", e, traceback.format_exc()
                )
                await event_q.put(("err", e))
            finally:
                await event_q.put(("done", _DONE))

        prod_task = asyncio.create_task(_produce())

        _stream_start = time.monotonic()
        _first_event_logged = False
        _poll_interval = _QUEUE_POLL_INTERVAL_S
        # Last model_progress emission — throttles the liveness signal emitted
        # when upstream events map to nothing. Only the swallowed-event branch
        # reads/updates the clock, keeping the mapped hot path free of it.
        _last_progress_ts = _stream_start

        try:
            while True:
                for _human_event in _drain_human_interaction_events():
                    yield _human_event
                try:
                    kind, payload = await asyncio.wait_for(
                        event_q.get(), timeout=_poll_interval
                    )
                except asyncio.TimeoutError:
                    if self._model_call_inflight:
                        # Model call in flight with a silent wire — count as
                        # liveness (throttled) so the inactivity watchdog waits
                        # for the model client's own timeout/retry outcome
                        # instead of misjudging the run as hung.
                        _now = time.monotonic()
                        if _now - _last_progress_ts >= _MODEL_PROGRESS_MIN_INTERVAL_S:
                            _last_progress_ts = _now
                            yield ("model_progress", None)
                            continue
                    yield ("heartbeat", None)
                    continue
                if kind in {"done", "err"}:
                    # A human answer wakes the tool before AgentScope queues its
                    # terminal event. Drain once more here so an immediately
                    # finishing reply cannot strand the frontend in submitted
                    # state waiting for user_question_resolved.
                    for _human_event in _drain_human_interaction_events():
                        yield _human_event
                    if kind == "err":
                        yield ("error", payload)
                    break
                if kind == _subagent_stream.QUEUE_KIND:
                    # Subagent bypass events — passed straight through, never into _map_event.
                    if not _first_event_logged:
                        _first_event_logged = True
                    yield ("subagent_event", payload)
                    continue
                # kind == "ev"
                steer_delivery = getattr(agent.state, "steer_delivery", None)
                if isinstance(steer_delivery, dict):
                    # SteerMiddleware appends the user instruction immediately
                    # before this next reasoning round. Emit one acknowledgement
                    # before mapping ModelCallStart so the queued-card UI can
                    # switch from "waiting" to "applied" deterministically.
                    agent.state.steer_delivery = None
                    yield ("steer_applied", dict(steer_delivery))
                reasoning_protocol = self._take_reasoning_protocol()
                if reasoning_protocol is not None:
                    yield ("reasoning_protocol", reasoning_protocol)
                _mapped_any = False
                async for out in self._map_event(payload):
                    if not _first_event_logged:
                        _ttfe = (time.monotonic() - _stream_start) * 1000
                        logger.info("[stream] TTFE: %.0fms, type=%s", _ttfe, out[0])
                        _first_event_logged = True
                    _mapped_any = True
                    yield out
                if not _mapped_any:
                    # The model is alive (an upstream event just arrived) but
                    # nothing was forwarded — typical of long tool-call-arg /
                    # suppressed-thinking streaming. Emit a throttled liveness
                    # signal so the inactivity watchdog doesn't misjudge a
                    # healthy run as hung.
                    _now = time.monotonic()
                    if _now - _last_progress_ts >= _MODEL_PROGRESS_MIN_INTERVAL_S:
                        _last_progress_ts = _now
                        yield ("model_progress", None)
        except Exception as e:  # noqa: BLE001
            yield ("error", e)
        finally:
            # Deregister the subagent bypass — prevents late events from being written into a finished run's event_q.
            try:
                _subagent_stream.detach(st.chat_id)
            except Exception:  # noqa: BLE001
                pass
            for _tid, _rec in list(self._pending_tool_calls.items()):
                try:
                    _started_mono = _rec.get("started_monotonic")
                    _dur = (
                        int((time.monotonic() - _started_mono) * 1000)
                        if _started_mono
                        else None
                    )
                    log_writer.schedule_tool_call_write(
                        {
                            # user_id/chat_id are taken explicitly from agent.state — contextvars are
                            # unreliable in the stream() generator frame (the agent runs in the context
                            # snapshot of _produce's create_task, while tool results are written in the
                            # generator's consumer frame; the two contexts don't sync), so _context_ids
                            # cannot be relied on.
                            "user_id": st.user_id or None,
                            "chat_id": st.chat_id or None,
                            "tool_name": _rec.get("tool_name", "unknown"),
                            "tool_call_id": _tid,
                            "tool_args": _rec.get("tool_args"),
                            "tool_result": None,
                            "status": "failed",
                            "error_message": "no tool_result received (stream ended)",
                            "duration_ms": _dur,
                            "started_at": _rec.get("started_at"),
                        }
                    )
                except Exception:
                    logger.debug("pending tool_call flush failed", exc_info=True)
            self._pending_tool_calls.clear()
            self._tool_delta_emit_buf.clear()
            self._tool_delta_last_emit.clear()
            try:
                _log_ctx.__exit__(None, None, None)
            except Exception:
                pass
            if not prod_task.done():

                async def _wait():
                    try:
                        await asyncio.wait_for(asyncio.shield(prod_task), timeout=10)
                    except asyncio.TimeoutError:
                        prod_task.cancel()
                        try:
                            await prod_task
                        except BaseException:
                            pass
                    except Exception:
                        pass

                asyncio.create_task(_wait())

    async def _map_event(self, ev: Any) -> AsyncIterator[Tuple[str, Any]]:
        """Map a single reply_stream event into 0..N SSE events."""
        nm = type(ev).__name__

        if nm == "TextBlockDeltaEvent":
            delta = getattr(ev, "delta", "") or ""
            if not delta:
                return
            # Normal state (enable_thinking): 2.0 events are already incremental — forward
            # directly, no accumulate+recompute needed (the frontend parses <think> itself),
            # avoiding an O(n) scan of the full answer on every delta.
            if self._enable_thinking:
                if self._round_index == 0:
                    self._first_round_text += delta
                yield ("text_delta", delta)
                return
            # Suppression state: <think> may span multiple deltas — accumulate, then strip out the answer after the closing tag.
            self._raw_text += delta
            answer, self._in_thinking = _strip_thinking_answer(
                self._raw_text, self._enable_thinking, self._in_thinking
            )
            if answer and answer != self._emitted_answer:
                out = (
                    answer[len(self._emitted_answer) :]
                    if answer.startswith(self._emitted_answer)
                    else answer
                )
                if out:
                    yield ("text_delta", out)
                self._emitted_answer = answer
            return

        if nm == "ThinkingBlockDeltaEvent":
            if self._enable_thinking:
                d = getattr(ev, "delta", "") or ""
                if d:
                    yield ("thinking_delta", d)
            return

        if nm == "ToolCallStartEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            name = getattr(ev, "tool_call_name", "") or "unknown"
            self._tool_name_buf[tid] = name
            self._tool_args_buf[tid] = ""
            self._tool_delta_emit_buf[tid] = ""
            self._tool_delta_last_emit[tid] = time.monotonic()
            self._pending_tool_calls[tid] = {
                "tool_name": name,
                "tool_args": None,
                "started_monotonic": time.monotonic(),
                "started_at": datetime.now(timezone.utc),
            }
            yield ("tool_call_start", {"name": name, "id": tid})
            return

        if nm == "ToolCallDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            delta = getattr(ev, "delta", "") or ""
            if not delta:
                return
            self._tool_args_buf[tid] = self._tool_args_buf.get(tid, "") + delta
            pending_delta = self._tool_delta_emit_buf.get(tid, "") + delta
            self._tool_delta_emit_buf[tid] = pending_delta
            now = time.monotonic()
            last_emit = self._tool_delta_last_emit.get(tid, now)
            if (
                len(pending_delta) >= _TOOL_CALL_DELTA_FLUSH_CHARS
                or now - last_emit >= _TOOL_CALL_DELTA_FLUSH_INTERVAL_S
            ):
                self._tool_delta_emit_buf[tid] = ""
                self._tool_delta_last_emit[tid] = now
                yield (
                    "tool_call_delta",
                    {
                        "name": self._tool_name_buf.get(tid, "unknown"),
                        "id": tid,
                        "delta": pending_delta,
                    },
                )
            return

        if nm == "ToolCallEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            name = self._tool_name_buf.pop(tid, "unknown")
            args_str = self._tool_args_buf.pop(tid, "")
            pending_delta = self._tool_delta_emit_buf.pop(tid, "")
            self._tool_delta_last_emit.pop(tid, None)
            if pending_delta:
                yield (
                    "tool_call_delta",
                    {"name": name, "id": tid, "delta": pending_delta},
                )
            import json

            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {"_raw": args_str}
            rec = self._pending_tool_calls.get(tid)
            if rec is not None:
                rec["tool_args"] = args
            try:
                from orchestration.tool_callbacks import note_tool_call

                note_tool_call(
                    self.__dict__.setdefault("_tool_warn_state", {}), name, args
                )
            except Exception:  # noqa: BLE001
                pass
            yield ("tool_call", {"name": name, "args": args, "id": tid})
            return

        if nm == "ToolResultTextDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            self._tool_result_buf[tid] = self._tool_result_buf.get(tid, "") + (
                getattr(ev, "delta", "") or ""
            )
            return

        if nm == "ToolResultEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            content = self._tool_result_buf.pop(tid, "")
            raw_state = getattr(ev, "state", "") or ""
            state = str(getattr(raw_state, "value", raw_state) or "")
            pending = self._pending_tool_calls.pop(tid, None)
            name = (
                getattr(ev, "tool_call_name", "")
                or (pending or {}).get("tool_name")
                or self._tool_name_buf.get(tid)
                or "unknown"
            )
            try:
                is_error = state in {
                    "error",
                    "denied",
                    "interrupted",
                } or _looks_like_tool_error(content)
                effect_link = dict(
                    (getattr(self.agent.state, "tool_effect_links", None) or {}).pop(
                        tid, {}
                    )
                )
                started_mono = pending.get("started_monotonic") if pending else None
                duration_ms = (
                    int((time.monotonic() - started_mono) * 1000)
                    if started_mono
                    else None
                )
                log_writer.schedule_tool_call_write(
                    {
                        # See the matching comment in _produce: carry user_id/chat_id explicitly, never rely on contextvars.
                        "user_id": self.agent.state.user_id or None,
                        "chat_id": self.agent.state.chat_id or None,
                        "run_id": self.agent.state.run_id or None,
                        "effect_id": effect_link.get("effect_id"),
                        "result_id": effect_link.get("result_id"),
                        "tool_name": (pending or {}).get("tool_name") or name,
                        "tool_call_id": tid,
                        "tool_args": (pending or {}).get("tool_args"),
                        "tool_result": content,
                        "status": "failed" if is_error else "success",
                        "error_message": content if is_error else None,
                        "duration_ms": duration_ms,
                        "started_at": (pending or {}).get("started_at"),
                    }
                )
            except Exception:  # noqa: BLE001
                logger.debug("tool_call log persist failed", exc_info=True)
            yield (
                "tool_result",
                {"name": name, "id": tid, "content": content, "status": state},
            )
            return

        if nm == "ModelCallStartEvent":
            # Marks the wire as "waiting on the model" — see _model_call_inflight.
            self._model_call_inflight = True
            return

        if nm == "ModelCallEndEvent":
            self._model_call_inflight = False
            _prompt_tokens = int(getattr(ev, "input_tokens", 0) or 0)
            _completion_tokens = int(getattr(ev, "output_tokens", 0) or 0)
            self._usage_records.append(
                {
                    "prompt_tokens": _prompt_tokens,
                    "completion_tokens": _completion_tokens,
                }
            )
            # Hand the provider's real usage to the agent so the next step
            # boundary measures context occupancy the same way the post-turn
            # trigger does, instead of falling back to the byte estimate that
            # over-counts Chinese by ~1.7x (see CompactingAgent._measure_context_tokens).
            _observe = getattr(self.agent, "observe_context_tokens", None)
            if callable(_observe):
                try:
                    _observe(_prompt_tokens, _completion_tokens)
                except Exception as _obs_exc:  # noqa: BLE001
                    logger.debug("[compaction] usage observation skipped: %s", _obs_exc)
            # 首轮权威判定：思考模式下整轮正文没有出现 </think> → 该模型不内联思考
            # （结构化 reasoning 通道，或本轮确实没思考）。补发协议标记，前端据此
            # 把误当思考缓冲/展示的正文重归正文区（bug：无思考时正文进思考块）。
            # 只看首轮——混合形态模型工具后省略 <think> 属既有启发式管辖，不在此误判。
            if (
                self._enable_thinking
                and self._round_index == 0
                and not self._structured_marker_sent
                and self._first_round_text
                and "</think>" not in self._first_round_text
            ):
                self._structured_marker_sent = True
                yield ("reasoning_protocol", {"structured_reasoning": True})
            self._round_index += 1
            self._first_round_text = ""
            # New model call round → reset answer accumulation (the next text segment computes deltas from scratch)
            self._raw_text = ""
            self._emitted_answer = ""
            self._in_thinking = False
            return

        if nm == "RequireUserConfirmEvent":
            # 2.0 native HITL (distinct from the myspace gate); our tools default to ALLOW, so this rarely triggers.
            try:
                yield (
                    "file_confirm",
                    {
                        "reply_id": getattr(ev, "reply_id", ""),
                        "tool_calls": [
                            {
                                "id": getattr(tc, "id", ""),
                                "name": getattr(tc, "name", ""),
                                "input": getattr(tc, "input", ""),
                            }
                            for tc in (getattr(ev, "tool_calls", []) or [])
                        ],
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            return

        if nm == "ExceedMaxItersEvent":
            yield (
                "error",
                {"kind": "exceed_max_iters", "name": getattr(ev, "name", "")},
            )
            return

        # Everything else is never forwarded, internal only: lifecycle (ReplyStart/End,
        # the various *Start/*End) + DataBlockStart/Delta (model-produced
        # image_chunk — currently no downstream consumer and the configured models only take
        # images as input, so intentionally dropped; add a branch when support is needed).
        return

    async def shutdown(self):
        """Close transient (per-request) MCP clients."""
        from core.llm.mcp_manager import close_clients

        try:
            await close_clients(self.mcp_clients)
        except Exception as exc:
            logger.debug("StreamingAgent.shutdown: close_clients error: %s", exc)
        self.mcp_clients = []
