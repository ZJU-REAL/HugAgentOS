"""Route job control onto the long-lived service loop, never a model worker loop."""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def bind():
    current = asyncio.get_running_loop()
    with process_state().guard:
        if process_state().loop is not None and process_state().loop.is_running() and process_state().loop is not current:
            raise RuntimeError("Job service is already bound to another live loop")
        process_state().loop = current


def service_loop():
    from core.sandbox import get_sandbox_provider

    with process_state().guard:
        if process_state().loop is None or process_state().loop.is_closed():
            provider_loop = getattr(get_sandbox_provider(), "_service_loop", None)
            process_state().loop = provider_loop
        if process_state().loop is None:
            raise RuntimeError("Job service is not initialized on the application loop")
        if not process_state().loop.is_running():
            raise RuntimeError("Job service loop is not running")
        return process_state().loop


def _retain(coro):
    task = asyncio.create_task(coro)
    process_state().tasks.add(task)

    def done(finished):
        process_state().tasks.discard(finished)
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


@dataclass
class OwnerState:
    loop: object = None
    guard: object = field(default_factory=lambda: threading.Lock())
    tasks: object = field(default_factory=lambda: set())


_STATE = OwnerState()


def process_state() -> OwnerState:
    return _STATE
