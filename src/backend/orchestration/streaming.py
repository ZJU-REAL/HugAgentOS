"""Streaming agent wrapper for AgentScope 2.0.

Consumes ``agent.reply_stream(...)`` (replaces the 1.x msg_queue) and maps the 25
fine-grained EventType into our 8 SSE events:
- ("text_delta", str)      - incremental answer text
- ("thinking_delta", str)  - incremental reasoning
- ("tool_pending", dict)   - tool call started (args still streaming)
- ("tool_call", dict)      - tool invocation complete
- ("tool_result", dict)    - tool invocation result
- ("file_confirm", dict)   - myspace write confirmation (in-house ContextVar gate, distinct from native HITL)
- ("heartbeat", None)      - silence heartbeat
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

from core.services import log_service as log_writer
from core.infra.logging import LogContext
from core.llm.message_compat import session_to_msgs

logger = logging.getLogger(__name__)


_HTTP_ERR_RE = re.compile(r"HTTP\s*[45]\d\d")


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
    if s.startswith(("Error executing tool", "Error: ", "Traceback (most recent call last)")):
        return True
    if "validation error for" in head:
        return True
    if "不存在或已删除" in s or "无权访问" in head:
        return True
    if _HTTP_ERR_RE.search(s):
        return True
    return False


def _strip_thinking_answer(raw: str, enable_thinking: bool, in_thinking: bool) -> Tuple[str, bool]:
    """Extract the user-visible "answer" portion from the accumulated raw text.

    Returns (answer, new_in_thinking). When enable_thinking=True, returns as-is (the frontend
    parses <think> itself); otherwise suppresses the <think>…</think> span and returns only
    the content after the closing tag.
    """
    if enable_thinking:
        return raw, False
    last_end = raw.rfind("</think>")
    if last_end != -1:
        return raw[last_end + len("</think>"):], False
    if "<think>" in raw or in_thinking:
        return "", True
    return raw, False


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
        # tool_id → accumulated result text (ToolResultTextDelta)
        self._tool_result_buf: Dict[str, str] = {}
        # Accumulated answer text (for dedup in the <think>-suppression scenario)
        self._raw_text = ""
        self._emitted_answer = ""
        self._in_thinking = False

    def get_usage(self) -> Dict[str, int]:
        total_prompt = sum(r.get("prompt_tokens", 0) for r in self._usage_records)
        total_completion = sum(r.get("completion_tokens", 0) for r in self._usage_records)
        last = self._usage_records[-1] if self._usage_records else {}
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "llm_call_count": len(self._usage_records),
            # True end-of-turn context occupancy ≈ prompt+completion of the last LLM call.
            # total_tokens is the whole-turn billing measure — the tool loop resends the full
            # context every round, so prompt is counted repeatedly and cannot be used to judge
            # window occupancy (compaction triggering uses context_tokens).
            "context_tokens": int(last.get("prompt_tokens", 0) or 0)
            + int(last.get("completion_tokens", 0) or 0),
        }

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

        _log_ctx = LogContext(user_id=st.user_id or None, chat_id=st.chat_id or None)
        _log_ctx.__enter__()

        # Load history into context (excluding the last user message — reply_stream's inputs carries it, avoiding duplication)
        history = list(session_messages)
        last_user_content = ""
        if history and history[-1].get("role") in ("user", "human"):
            last_user_content = history.pop().get("content", "")
        if history:
            try:
                agent.state.context.extend(session_to_msgs(history))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[stream] load history failed: %s", exc)

        user_msg: Optional[Msg] = None
        if last_user_content:
            from core.llm.message_compat import _wrap_content
            user_msg = Msg(name="user", role="user",
                           content=_wrap_content(last_user_content))

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
                logger.error("Agent reply_stream failed: %r\n%s", e, traceback.format_exc())
                await event_q.put(("err", e))
            finally:
                await event_q.put(("done", _DONE))

        prod_task = asyncio.create_task(_produce())

        _stream_start = time.monotonic()
        _first_event_logged = False
        _poll_interval = 3.0

        try:
            while True:
                for _cf in _drain_confirm_signals():
                    # The same ui_signals queue carries two kinds of signals: write confirmation → file_confirm,
                    # site-building three-way design choice → design_pick (payload carries question/options).
                    _et = (
                        "design_pick"
                        if (_cf or {}).get("kind") == _mc.KIND_DESIGN_PICK
                        else "file_confirm"
                    )
                    yield (_et, _cf)
                try:
                    kind, payload = await asyncio.wait_for(event_q.get(), timeout=_poll_interval)
                except asyncio.TimeoutError:
                    yield ("heartbeat", None)
                    continue
                if kind == "done":
                    break
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
                async for out in self._map_event(payload):
                    if not _first_event_logged:
                        _ttfe = (time.monotonic() - _stream_start) * 1000
                        logger.info("[stream] TTFE: %.0fms, type=%s", _ttfe, out[0])
                        _first_event_logged = True
                    yield out
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
                    _dur = int((time.monotonic() - _started_mono) * 1000) if _started_mono else None
                    log_writer.schedule_tool_call_write({
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
                    })
                except Exception:
                    logger.debug("pending tool_call flush failed", exc_info=True)
            self._pending_tool_calls.clear()
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
                yield ("text_delta", delta)
                return
            # Suppression state: <think> may span multiple deltas — accumulate, then strip out the answer after the closing tag.
            self._raw_text += delta
            answer, self._in_thinking = _strip_thinking_answer(
                self._raw_text, self._enable_thinking, self._in_thinking
            )
            if answer and answer != self._emitted_answer:
                out = (
                    answer[len(self._emitted_answer):]
                    if answer.startswith(self._emitted_answer) else answer
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
            self._pending_tool_calls[tid] = {
                "tool_name": name,
                "tool_args": None,
                "started_monotonic": time.monotonic(),
                "started_at": datetime.now(timezone.utc),
            }
            yield ("tool_pending", {"reason": "tool_call_start", "tool_name": name})
            return

        if nm == "ToolCallDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            self._tool_args_buf[tid] = self._tool_args_buf.get(tid, "") + (getattr(ev, "delta", "") or "")
            return

        if nm == "ToolCallEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            name = self._tool_name_buf.pop(tid, "unknown")
            args_str = self._tool_args_buf.pop(tid, "")
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
                note_tool_call(self.__dict__.setdefault("_tool_warn_state", {}), name, args)
            except Exception:  # noqa: BLE001
                pass
            yield ("tool_call", {"name": name, "args": args, "id": tid})
            return

        if nm == "ToolResultTextDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            self._tool_result_buf[tid] = self._tool_result_buf.get(tid, "") + (getattr(ev, "delta", "") or "")
            return

        if nm == "ToolResultEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            content = self._tool_result_buf.pop(tid, "")
            state = str(getattr(ev, "state", "") or "")
            pending = self._pending_tool_calls.pop(tid, None)
            name = (getattr(ev, "tool_call_name", "") or (pending or {}).get("tool_name")
                    or self._tool_name_buf.get(tid) or "unknown")
            try:
                is_error = state == "error" or _looks_like_tool_error(content)
                started_mono = pending.get("started_monotonic") if pending else None
                duration_ms = int((time.monotonic() - started_mono) * 1000) if started_mono else None
                log_writer.schedule_tool_call_write({
                    # See the matching comment in _produce: carry user_id/chat_id explicitly, never rely on contextvars.
                    "user_id": self.agent.state.user_id or None,
                    "chat_id": self.agent.state.chat_id or None,
                    "tool_name": (pending or {}).get("tool_name") or name,
                    "tool_call_id": tid,
                    "tool_args": (pending or {}).get("tool_args"),
                    "tool_result": content,
                    "status": "failed" if is_error else "success",
                    "error_message": content if is_error else None,
                    "duration_ms": duration_ms,
                    "started_at": (pending or {}).get("started_at"),
                })
            except Exception:  # noqa: BLE001
                logger.debug("tool_call log persist failed", exc_info=True)
            yield ("tool_result", {"name": name, "id": tid, "content": content})
            return

        if nm == "ModelCallEndEvent":
            self._usage_records.append({
                "prompt_tokens": int(getattr(ev, "input_tokens", 0) or 0),
                "completion_tokens": int(getattr(ev, "output_tokens", 0) or 0),
            })
            # New model call round → reset answer accumulation (the next text segment computes deltas from scratch)
            self._raw_text = ""
            self._emitted_answer = ""
            self._in_thinking = False
            return

        if nm == "RequireUserConfirmEvent":
            # 2.0 native HITL (distinct from the myspace gate); our tools default to ALLOW, so this rarely triggers.
            try:
                yield ("file_confirm", {
                    "reply_id": getattr(ev, "reply_id", ""),
                    "tool_calls": [
                        {"id": getattr(tc, "id", ""), "name": getattr(tc, "name", ""),
                         "input": getattr(tc, "input", "")}
                        for tc in (getattr(ev, "tool_calls", []) or [])
                    ],
                })
            except Exception:  # noqa: BLE001
                pass
            return

        if nm == "ExceedMaxItersEvent":
            yield ("error", {"kind": "exceed_max_iters", "name": getattr(ev, "name", "")})
            return

        # Everything else is never forwarded, internal only: lifecycle (ReplyStart/End,
        # ModelCallStart, the various *Start/*End) + DataBlockStart/Delta (model-produced
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
