"""Join owned asynchronous work without abandoning cleanup on repeated cancellation."""

from __future__ import annotations

import asyncio


async def settle_task(future):
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


async def cancel_and_join(task: asyncio.Task) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()
    await settle_task(asyncio.gather(task, return_exceptions=True))
