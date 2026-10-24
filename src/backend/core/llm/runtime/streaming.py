"""Wrap an AgentScope agent so it streams SSE events."""
from __future__ import annotations
from contextlib import aclosing
from core.llm.runtime import stream_loop
from core.llm.runtime import stream_events
from core.llm.runtime.delta_coalescer import DeltaCoalescer
import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from agentscope.agent import Agent
from core.llm.mcp.lifetime import MCPResource
import logging
logger = logging.getLogger(__name__)

class StreamingAgent:
    """Wraps an AgentScope 2.0 ``Agent`` to produce streaming SSE events via reply_stream."""

    def __init__(
        self,
        agent: Agent,
        mcp_clients: List[MCPResource],
    ):
        self._producer_task = None
        self.agent = agent
        self.mcp_clients = mcp_clients
        self._enable_thinking = False
        # usage: accumulated from ModelCallEndEvent
        self._usage_records: List[Dict[str, int]] = []
        self._context_usage: Optional[Dict[str, Any]] = None
        # tool_id → {tool_name, tool_args(str), started_monotonic, started_at}
        self._pending_tool_calls: Dict[str, Dict[str, Any]] = {}
        # tool_id → name (recorded at ToolCallStart), tool_id → accumulated args string (ToolCallDelta)
        self._tool_name_buf: Dict[str, str] = {}
        self._tool_args_buf: Dict[str, str] = {}
        # Merges text / thinking / tool-argument deltas before they leave _map_event.
        self._deltas = DeltaCoalescer()
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

    def _drain_model_steps(self) -> List[Dict[str, Any]]:
        state = getattr(self.agent, "state", None)
        pending = getattr(state, "pending_model_steps", None)
        if not pending:
            return []
        drained = list(pending)
        state.pending_model_steps = []
        return drained

    async def _tool_result_step(self, tool_call_id: str) -> Optional[Dict[str, Any]]:
        """Record the tool result exactly as AgentScope saved it into context.

        ``_execute_tool_call`` appends the (already bounded) ToolResultBlock to
        the last assistant message before it yields ``ToolResultEndEvent``, so
        by the time the event reaches this consumer the block is there.

        Recording a result that carries media writes those bytes to the blob
        table (see :mod:`core.llm.runtime.tool_media_store`), so the work goes to a
        thread instead of blocking the event loop.
        """
        from core.llm.runtime.model_steps import record_tool_result_step

        context = getattr(getattr(self.agent, "state", None), "context", None) or []
        last = context[-1] if context else None
        if last is None or getattr(last, "role", "") != "assistant":
            logger.error("tool result %s ended with no assistant message in context", tool_call_id)
            return None
        match = None
        for block in last.get_content_blocks("tool_result"):
            if getattr(block, "id", "") == tool_call_id:
                match = block
        if match is None:
            logger.error("tool result %s ended but its block is not in context", tool_call_id)
            return None
        return await asyncio.to_thread(record_tool_result_step, match)

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
        total_completion = sum(r.get("completion_tokens", 0) for r in self._usage_records)
        last = self._usage_records[-1] if self._usage_records else {}
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "llm_call_count": len(self._usage_records),
            "context_tokens": int(last.get("prompt_tokens", 0) or 0)
            + int(last.get("completion_tokens", 0) or 0),
            "last_prompt_tokens": int(last.get("prompt_tokens", 0) or 0),
            "last_completion_tokens": int(last.get("completion_tokens", 0) or 0),
        }

    async def aget_usage(self) -> Dict[str, int]:
        """Derive usage from append-only attempts without blocking the event loop."""
        run_id = str(getattr(getattr(self.agent, "state", None), "run_id", "") or "")
        if run_id:
            try:
                from core.harness.ledger import HarnessUsageLedger

                attempts = await asyncio.wait_for(
                    asyncio.to_thread(HarnessUsageLedger().attempts, run_id),
                    timeout=1.0,
                )
                model_attempts = tuple(row for row in attempts if row.kind == "model")
                if model_attempts:
                    primary_attempts = tuple(
                        row
                        for row in model_attempts
                        if row.metadata.get("source") not in {"vision", "compaction", "followup"}
                    )
                    last = (primary_attempts or model_attempts)[-1]
                    return {
                        "prompt_tokens": sum(row.usage.prompt_tokens for row in model_attempts),
                        "completion_tokens": sum(
                            row.usage.completion_tokens for row in model_attempts
                        ),
                        "cache_read_tokens": sum(
                            row.usage.cache_read_tokens for row in model_attempts
                        ),
                        "cache_write_tokens": sum(
                            row.usage.cache_write_tokens for row in model_attempts
                        ),
                        "total_tokens": sum(row.usage.total_tokens for row in model_attempts),
                        "llm_call_count": len(model_attempts),
                        "context_tokens": last.usage.prompt_tokens + last.usage.completion_tokens,
                        "last_prompt_tokens": last.usage.prompt_tokens,
                        "last_completion_tokens": last.usage.completion_tokens,
                    }
            except Exception:
                logger.debug("usage ledger aggregate unavailable", exc_info=True)
        return self.get_usage()

    def get_context_usage(self, usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return the latest provider measurement or canonical fallback."""
        if self._context_usage and self._context_usage.get("exact"):
            return dict(self._context_usage)
        last = self._usage_records[-1] if self._usage_records else {}
        if usage:
            prompt = int(usage.get("last_prompt_tokens", 0) or 0)
            completion = int(usage.get("last_completion_tokens", 0) or 0)
        else:
            prompt = int(last.get("prompt_tokens", 0) or 0)
            completion = int(last.get("completion_tokens", 0) or 0)
        from core.llm.context.usage import build_context_usage_snapshot

        snapshot = build_context_usage_snapshot(
            self.agent,
            prompt_tokens=prompt,
            completion_tokens=completion,
            model_call_index=len(self._usage_records),
        )
        self._context_usage = snapshot
        return dict(snapshot)

    def restore_context_usage(self, snapshot: Optional[Dict[str, Any]]) -> None:
        """Restore a snapshot after a transient call that is not replayed next turn."""
        self._context_usage = dict(snapshot) if snapshot else None

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
            from core.llm.context.attachment_hook import supports_native_images
            from core.models.vision.attachments import image_attachments, transcribe_attachments

            from core.files import artifact_bytes
            images = image_attachments(list(getattr(st, "uploaded_files", None) or []), is_image=artifact_bytes.is_image)
            if not images or supports_native_images(getattr(st, "model_provider_id", "") or ""):
                return

            yield (
                "vision_progress",
                {
                    "status": "running",
                    "count": len(images),
                    "names": [f.get("name") or "图片" for f in images],
                },
            )
            from core.models.usage import model_usage_scope
            from core.harness.ledger import HarnessUsageLedger

            run_id = str(getattr(st, "run_id", "") or "")
            with model_usage_scope(run_id, HarnessUsageLedger()):
                result = await transcribe_attachments(
                    images, download_bytes=artifact_bytes.download_artifact_bytes,
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
                    "duration_seconds": round(result.duration_seconds, 2) if result else 0.0,
                },
            )
        except Exception as exc:  # noqa: BLE001 — fall back to inline transcription
            logger.warning("[stream] vision pre-pass failed, deferring to middleware: %s", exc)
            yield ("vision_progress", {"status": "done", "count": 0, "ok": False})

    async def stream(
        self,
        session_messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        *,
        effective_user_message: Optional[str] = None,
    ) -> AsyncIterator[Tuple[str, Any]]:
        async with aclosing(stream_loop.stream(self, session_messages, context, effective_user_message=effective_user_message)) as events:
            async for event in events:
                yield event

    async def _map_event(
        self, ev: Any, *, result_step: Optional[Dict[str, Any]] = None
    ) -> AsyncIterator[Tuple[str, Any]]:
        """Map one AgentScope event, with deltas merged by ``self._deltas``.

        A merged delta may still be buffered when this returns; whoever drives
        the stream flushes it through ``self._deltas`` (on its deadline, before
        its own events, and at the end).
        """
        async with aclosing(stream_events.map_event(self, ev, result_step=result_step)) as events:
            async for event in events:
                for out in self._deltas.feed(event):
                    yield out

    async def stop_producer(self):
        """Stop actual model/tool work before closing the stream or its clients."""
        from core.infra.task_lifecycle import cancel_and_join

        task = self._producer_task
        if task is not None:
            await cancel_and_join(task)
            if self._producer_task is task:
                self._producer_task = None

    async def shutdown(self):
        """Close transient (per-request) MCP clients."""
        from core.llm.mcp.manager import close_clients

        try:
            await self.stop_producer()
        finally:
            try:
                await close_clients(self.mcp_clients)
            finally:
                self.mcp_clients = []
