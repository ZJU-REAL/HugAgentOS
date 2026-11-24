"""Cancel a running tool when the user steers the run mid-call.

The tool stream is drained in its own task (so context variables set inside
the tool stay in one context) and raced against an interrupt waiter. When the
waiter wins, the tool task is cancelled — tools release their own resources on
``CancelledError``, as they do for a user stop — and an ``interrupted``
response takes the place of the tool result.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from agentscope.message import ToolResultState
from agentscope.tool._response import ToolResponse

from core.llm.context.rendering import render_text_block

TOOL_INTERRUPTED_NOTICE = (
    "用户追加了新指令；本工具调用在执行中被中止，结果不完整，"
    "等待模型按新指令重新规划。"
)

InterruptWaiter = Callable[[], Awaitable[Any]]


def interrupted_response() -> ToolResponse:
    return ToolResponse(
        content=[render_text_block(TOOL_INTERRUPTED_NOTICE)],
        state=ToolResultState.INTERRUPTED,
        metadata={"interrupted_by": "user_steer"},
    )


async def _cancel(task: Optional[asyncio.Future]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(BaseException):
        await task


async def interruptible(
    make_stream: Callable[[], AsyncIterator[Any]],
    wait_interrupt: Optional[InterruptWaiter],
) -> AsyncIterator[Any]:
    """Yield ``make_stream()``'s items; if ``wait_interrupt`` finishes first,
    cancel the stream and yield one ``interrupted_response()`` instead."""
    if wait_interrupt is None:
        async for item in make_stream():
            yield item
        return

    queue: asyncio.Queue = asyncio.Queue()

    async def _drain() -> None:
        async for item in make_stream():
            await queue.put(item)

    producer = asyncio.create_task(_drain())
    watcher: Optional[asyncio.Future] = asyncio.ensure_future(wait_interrupt())
    getter: Optional[asyncio.Future] = None
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            waiting = {getter, producer} | ({watcher} if watcher is not None else set())
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
                continue
            await _cancel(getter)
            if producer in done:
                producer.result()  # re-raise the tool's own failure
                while not queue.empty():
                    yield queue.get_nowait()
                return
            if watcher is not None and watcher.done():
                if watcher.cancelled() or watcher.exception() is not None:
                    # A broken waiter must never fail the tool; run it to the end.
                    watcher = None
                    continue
                await _cancel(producer)
                yield interrupted_response()
                return
    finally:
        await _cancel(getter)
        await _cancel(watcher)
        await _cancel(producer)
