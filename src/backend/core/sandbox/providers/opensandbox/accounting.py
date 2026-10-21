"""Complete local accounting before propagating caller cancellation."""
import asyncio


async def finish_accounting(action):
    task = asyncio.ensure_future(action)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result
