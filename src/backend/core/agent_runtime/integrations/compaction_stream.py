"""Bridge awaited compression into the existing run stream, including /compact."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing, suppress
from dataclasses import dataclass

from core.llm.context.safety import request_admission_scope
from core.chat.compaction.progress import progress_scope, report_outcome
from core.chat.compaction.budget_checkpoint import ContextStopCheckpoint

logger = logging.getLogger(__name__)


@dataclass
class _WorkflowChunk:
    event: dict
    consumed: asyncio.Future


def is_compact_command(text: str) -> bool:
    return text.strip() == "/compact"


async def stream_with_compaction(
    workflow, *, session_messages, user_message, raw_user_message, context
):
    queue = asyncio.Queue(maxsize=256)
    finished = object()
    chat_id = context.get("chat_id") or ""
    run_id = context.get("run_id") or ""
    latest_progress = None
    closing = False
    checkpoint = ContextStopCheckpoint()

    async def sink(event):
        nonlocal latest_progress
        # A subagent's private context must not be labelled as main context.
        if event.get("chat_id") == chat_id and event.get("run_id") == run_id:
            latest_progress = event
            if not closing:
                await queue.put(event)

    async def produce():
        try:
            with (
                request_admission_scope(context=context, checkpoint=checkpoint) as admission,
                progress_scope(sink, chat_id, run_id, context=context),
            ):
                if is_compact_command(raw_user_message):
                    from core.config.settings import settings
                    from core.chat.compaction.service import run_compaction

                    if not settings.compaction.enabled:
                        await report_outcome(chat_id, run_id, "manual", "skipped", "disabled")
                        text = "上下文压缩未启用。"
                    else:
                        replacement = await run_compaction(chat_id, run_id=run_id)
                        if replacement is not None:
                            text = "上下文已压缩，可以继续对话。"
                        elif latest_progress and latest_progress["status"] in (
                            "failed",
                            "interrupted",
                        ):
                            text = "上下文压缩未完成，请稍后输入 /compact 重试。"
                        else:
                            if latest_progress is None:
                                await report_outcome(
                                    chat_id, run_id, "manual", "skipped", "not_needed"
                                )
                            text = (
                                "已有上下文压缩任务正在执行，请稍后重试。"
                                if latest_progress.get("reason") == "busy"
                                else "当前上下文无需压缩。"
                            )
                    await queue.put({"type": "content", "delta": text})
                    await queue.put({"type": "meta", "manual_compaction": True})
                else:
                    async with aclosing(
                        workflow(
                            session_messages=session_messages,
                            user_message=user_message,
                            context=context,
                        )
                    ) as stream:
                        async for chunk in stream:
                            if chunk.get("type") == "meta":
                                admission.check()
                            consumed = asyncio.get_running_loop().create_future()
                            await queue.put(_WorkflowChunk(chunk, consumed))
                            # Preserve the original yield boundary: downstream
                            # persistence must finish before workflow side effects.
                            await consumed
                            checkpoint.observe(chunk)
                    admission.check()
        except asyncio.CancelledError as cancellation:
            if not closing:
                await queue.put(cancellation)
            raise
        except Exception as exc:
            stopped = admission.stopped
            if stopped is not None:
                if not stopped.saved:
                    try:
                        await asyncio.to_thread(checkpoint.save, context, stopped)
                    except Exception as checkpoint_error:
                        exc = checkpoint_error
                    else:
                        exc = stopped
                else:
                    exc = stopped
            if not closing:
                await queue.put(exc)
            else:
                logger.warning("compaction_stream_cleanup_failed", exc_info=True)
        finally:
            # Cancellation must not block while the consumer is closing.
            if not closing and not asyncio.current_task().cancelling():
                await queue.put(finished)

    task = asyncio.create_task(produce(), name=f"compaction_stream:{run_id}")
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield {
                    "type": (
                        "model_progress"
                        if latest_progress and latest_progress["status"] == "running"
                        else "heartbeat"
                    )
                }
                continue
            if item is finished:
                break
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _WorkflowChunk):
                yield item.event
                if not item.consumed.done():
                    item.consumed.set_result(None)
            else:
                yield item
    finally:
        closing = True
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
