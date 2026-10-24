"""Cancellation-safe bridge for durable effects executed in worker threads."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")


async def run_effect(fn: Callable[..., T], *args, **kwargs) -> T:
    """Run a sync effect without releasing its lease while its thread is alive.

    Cancelling an asyncio Future cannot stop a function already running in an
    executor thread.  Shield the Future, and if the owner is cancelled, wait
    for that thread to finish before propagating cancellation.  The outbox
    heartbeat therefore remains alive and shutdown cannot release the lease
    while the old external write is still in flight.
    """

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, partial(fn, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await future
        except Exception as fallback_error:
            # Cancellation remains the owner-visible outcome.  The durable row
            # is released/retried only after this thread has actually stopped.
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.memory.executor:run_effect', fallback_error)
        raise
