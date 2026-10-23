"""Drive one agent reply and yield its SSE events."""
from __future__ import annotations
from core.llm.runtime import streaming_constants
from core.harness import log_writer
import asyncio
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from agentscope.message import Msg
from core.infra.logging import LogContext
from core.llm.context.rendering import next_request_sequence, render_context_item, render_session_input
from core.llm.context.ir_constants import KIND_USER_INPUT, POLICY_NEVER, SESSION_CONTEXT_META_KEY
from core.llm.context.items import ContextItem
from core.llm.runtime.message_compat import session_to_msgs
import logging
logger = logging.getLogger(__name__)

async def stream(
    self,
    session_messages: List[Dict[str, Any]],
    context: Dict[str, Any],
    *,
    effective_user_message: Optional[str] = None,
) -> AsyncIterator[Tuple[str, Any]]:
    agent = self.agent

    # Commands keep their short spelling in persisted history, while this
    # request must execute the expanded instructions. Copy the current row
    # so neither stored history nor earlier user turns are rewritten.
    if effective_user_message is not None:
        session_messages = list(session_messages)
        if session_messages and session_messages[-1].get("role") in ("user", "human"):
            session_messages[-1] = {
                **session_messages[-1],
                "content": effective_user_message,
            }
        else:
            session_messages.append({"role": "user", "content": effective_user_message})

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
    from core.llm.tools import confirmation as _mc

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
    from core.llm.subagents import stream as _subagent_stream

    _subagent_stream.attach(st.chat_id, asyncio.get_running_loop(), event_q)

    async def _produce():
        try:
            async for ev in agent.reply_stream(inputs=user_msg):
                result_step = None
                if type(ev).__name__ == "ToolResultEndEvent":
                    tid = getattr(ev, "tool_call_id", "") or ""
                    # Snapshot while the producer is still at the tool
                    # boundary, before another model step/steer/compaction
                    # can replace the live context.
                    result_step = await self._tool_result_step(tid)
                    if result_step is None:
                        raise RuntimeError(f"Tool result {tid} is missing from model context")
                await event_q.put(("ev", (ev, result_step)))
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            import traceback

            logger.error("Agent reply_stream failed: %r\n%s", e, traceback.format_exc())
            await event_q.put(("err", e))
        finally:
            await event_q.put(("done", _DONE))

    prod_task = asyncio.create_task(_produce())
    self._producer_task = prod_task

    _stream_start = time.monotonic()
    _first_event_logged = False
    _poll_interval = streaming_constants.QUEUE_POLL_INTERVAL_S
    # Last model_progress emission — throttles the liveness signal emitted
    # when upstream events map to nothing. Only the swallowed-event branch
    # reads/updates the clock, keeping the mapped hot path free of it.
    _last_progress_ts = _stream_start

    # Every event leaves through the coalescer, so a buffered delta is always
    # flushed before whatever follows it and the stream keeps its order.
    deltas = self._deltas
    try:
        while True:
            for progress in st.pending_mcp_progress:
                if progress.get("tool_id") in self._pending_tool_calls:
                    for out in deltas.feed(("tool_progress", progress)):
                        yield out
            st.pending_mcp_progress = []
            for _human_event in _drain_human_interaction_events():
                for out in deltas.feed(_human_event):
                    yield out
            _timeout = _poll_interval
            _due = deltas.deadline()
            if _due is not None:
                _timeout = min(_timeout, max(0.0, _due - time.monotonic()))
            try:
                kind, payload = await asyncio.wait_for(event_q.get(), timeout=_timeout)
            except asyncio.TimeoutError:
                _flushed = deltas.flush_due()
                if _flushed:
                    for out in _flushed:
                        yield out
                    continue
                if _due is not None:
                    continue
                if self._model_call_inflight:
                    # Model call in flight with a silent wire — count as
                    # liveness (throttled) so the inactivity watchdog waits
                    # for the model client's own timeout/retry outcome
                    # instead of misjudging the run as hung.
                    _now = time.monotonic()
                    if _now - _last_progress_ts >= streaming_constants.MODEL_PROGRESS_MIN_INTERVAL_S:
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
                    for out in deltas.feed(_human_event):
                        yield out
                for out in deltas.drain():
                    yield out
                if kind == "err":
                    yield ("error", payload)
                break
            if kind == _subagent_stream.QUEUE_KIND:
                # Subagent bypass events — passed straight through, never into _map_event.
                if not _first_event_logged:
                    _first_event_logged = True
                for out in deltas.feed(("subagent_event", payload)):
                    yield out
                continue
            # kind == "ev"
            payload, result_step = payload
            steer_delivery = getattr(agent.state, "steer_delivery", None)
            if isinstance(steer_delivery, dict):
                # SteerMiddleware appends the user instruction immediately
                # before this next reasoning round. Emit one acknowledgement
                # before mapping ModelCallStart so the queued-card UI can
                # switch from "waiting" to "applied" deterministically.
                agent.state.steer_delivery = None
                for out in deltas.feed(("steer_applied", dict(steer_delivery))):
                    yield out
            reasoning_protocol = self._take_reasoning_protocol()
            if reasoning_protocol is not None:
                for out in deltas.feed(("reasoning_protocol", reasoning_protocol)):
                    yield out
            _mapped_any = False
            async for out in self._map_event(payload, result_step=result_step):
                if not _first_event_logged:
                    _ttfe = (time.monotonic() - _stream_start) * 1000
                    logger.info("[stream] TTFE: %.0fms, type=%s", _ttfe, out[0])
                    _first_event_logged = True
                _mapped_any = True
                yield out
            if not _mapped_any and deltas.deadline() is None:
                # The model is alive (an upstream event just arrived) but
                # nothing was forwarded — typical of long tool-call-arg /
                # suppressed-thinking streaming. Emit a throttled liveness
                # signal so the inactivity watchdog doesn't misjudge a
                # healthy run as hung.
                _now = time.monotonic()
                if _now - _last_progress_ts >= streaming_constants.MODEL_PROGRESS_MIN_INTERVAL_S:
                    _last_progress_ts = _now
                    yield ("model_progress", None)
    except Exception as e:  # noqa: BLE001
        for out in deltas.drain():
            yield out
        yield ("error", e)
    finally:
        await self.stop_producer()
        # Deregister the subagent bypass — prevents late events from being written into a finished run's event_q.
        try:
            _subagent_stream.detach(st.chat_id)
        except Exception as fallback_error:  # noqa: BLE001
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.llm.runtime.stream_loop:stream', fallback_error)
        for _tid, _rec in list(self._pending_tool_calls.items()):
            try:
                _started_mono = _rec.get("started_monotonic")
                _dur = int((time.monotonic() - _started_mono) * 1000) if _started_mono else None
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
        deltas.reset()
        try:
            _log_ctx.__exit__(None, None, None)
        except Exception as fallback_error:
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.llm.runtime.stream_loop:stream', fallback_error)
