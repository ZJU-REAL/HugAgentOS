"""Map AgentScope reply events to SSE events."""
from __future__ import annotations
from core.llm.runtime.stream_text import looks_like_tool_error as _looks_like_tool_error, strip_thinking_answer as _strip_thinking_answer
from core.harness import log_writer
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Tuple
import logging
logger = logging.getLogger(__name__)

async def map_event(
    self, ev: Any, *, result_step: Optional[Dict[str, Any]] = None
) -> AsyncIterator[Tuple[str, Any]]:
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
        yield (
            "tool_call_delta",
            {"name": self._tool_name_buf.get(tid, "unknown"), "id": tid, "delta": delta},
        )
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
            from core.llm.runtime.tool_callbacks import note_tool_call

            note_tool_call(self.__dict__.setdefault("_tool_warn_state", {}), name, args)
        except Exception as fallback_error:  # noqa: BLE001
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.llm.runtime.stream_events:map_event', fallback_error)
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
        if result_step is None:
            result_step = await self._tool_result_step(tid)
        if result_step is None:
            raise RuntimeError(f"Tool result {tid} is missing from model context")
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
                (getattr(self.agent.state, "tool_effect_links", None) or {}).pop(tid, {})
            )
            started_mono = pending.get("started_monotonic") if pending else None
            duration_ms = (
                int((time.monotonic() - started_mono) * 1000) if started_mono else None
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
        # Persist the canonical result before any UI filtering (notably
        # update_plan). The consumer must finish this event before the
        # display-only result can announce completion.
        yield ("model_step", result_step)
        yield (
            "tool_result",
            {
                "name": name,
                "id": tid,
                "content": content,
                "status": state,
            },
        )
        return

    if nm == "ModelCallStartEvent":
        # Marks the wire as "waiting on the model" — see _model_call_inflight.
        self._model_call_inflight = True
        # Agent construction and manifest-backed tool assembly happen before
        # this event. Surface the provider-dispatch boundary accurately.
        yield ("model_call_start", None)
        return

    if nm == "ModelCallEndEvent":
        self._model_call_inflight = False
        # The hook adapter recorded this response before the event was
        # queued; hand the record downstream in the same order.
        for step in self._drain_model_steps():
            yield ("model_step", step)
        _prompt_tokens = int(getattr(ev, "input_tokens", 0) or 0)
        _completion_tokens = int(getattr(ev, "output_tokens", 0) or 0)
        self._usage_records.append(
            {
                "prompt_tokens": _prompt_tokens,
                "completion_tokens": _completion_tokens,
            }
        )
        from core.llm.context.usage import build_context_usage_snapshot

        self._context_usage = build_context_usage_snapshot(
            self.agent,
            prompt_tokens=_prompt_tokens,
            completion_tokens=_completion_tokens,
            model_call_index=len(self._usage_records),
        )
        # Compaction usage is anchored by CompactingAgent._save_to_context
        # after the response is saved. AgentScope emits this event first;
        # observing here would count that response again as new content.
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
        yield ("context_usage", dict(self._context_usage))
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
        except Exception as fallback_error:  # noqa: BLE001
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.llm.runtime.stream_events:map_event', fallback_error)
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
