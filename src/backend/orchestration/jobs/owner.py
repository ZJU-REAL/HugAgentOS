"""Route job control onto the long-lived service loop, never a model worker loop."""

import asyncio
import functools
import logging
import threading

logger = logging.getLogger(__name__)
_loop = None
_guard = threading.Lock()
_tasks = set()


def bind():
    global _loop
    current = asyncio.get_running_loop()
    with _guard:
        if _loop is not None and _loop.is_running() and _loop is not current:
            raise RuntimeError("Job service is already bound to another live loop")
        _loop = current


def service_loop():
    global _loop
    from core.sandbox import get_sandbox_provider

    with _guard:
        if _loop is None or _loop.is_closed():
            provider_loop = getattr(get_sandbox_provider(), "_service_loop", None)
            _loop = provider_loop
        if _loop is None:
            raise RuntimeError("Job service is not initialized on the application loop")
        if not _loop.is_running():
            raise RuntimeError("Job service loop is not running")
        return _loop


def _retain(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def done(finished):
        _tasks.discard(finished)
        if not finished.cancelled() and finished.exception():
            logger.warning("Job control operation failed: %s", type(finished.exception()).__name__)

    task.add_done_callback(done)
    return task


def owned(func):
    """Cancellation of a caller must not abandon an uncertain launch."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        target = service_loop()

        async def operation():
            return await asyncio.shield(_retain(func(*args, **kwargs)))

        if target is asyncio.get_running_loop():
            return await operation()
        future = asyncio.run_coroutine_threadsafe(operation(), target)
        return await asyncio.shield(asyncio.wrap_future(future))

    return wrapper
