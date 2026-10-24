"""Replay and follow durable run events from the requested offset."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict

import core.chat.runtime.followups as followups
import core.runs.events.store as store
import core.runs.events.writer as writer
import core.runs.runtime as runtime
import core.runs.settings as settings
import redis.exceptions as _redis_exceptions
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def follow_run(run_id: str, *, from_offset: int = 0) -> AsyncIterator[Dict[str, Any]]:
    "Read the run's event log, delivering events after ``from_offset``.\n\n    Flow:\n    1. Replay everything recorded so far; deliver those with ``_offset > from_offset``\n    2. Block on the log waiting for new events\n    3. Stop when a ``__terminal__``-typed event arrives\n    4. Wait timed out + run in a terminal state → drain once more, then exit\n       gracefully (backstop so the follower never hangs forever)\n\n    Which log backs this — Redis Streams or the in-process one — is the\n    deployment's choice; see :mod:`core.runs.events.store`.\n"
    events = writer._events()

    run = runtime.get_run(run_id)
    if run is None:
        # The chats.py route layer should have validated this already; this is a second line of defense
        raise runtime.ChatRunNotFound(f"chat run {run_id} not found")

    cursor = store.START

    # ── Phase 1: replay historical events ──
    try:
        history = await events.read(run_id)
    except Exception as exc:
        logger.warning("chat_run_xrange_failed", run_id=run_id, error=str(exc), exc_info=exc)
        history = []

    delivered, cursor, terminal_seen = followups._drain(history, from_offset, cursor)
    for event in delivered:
        yield event
    if terminal_seen:
        return

    # ── Phase 2: blocking tail ──
    while True:
        try:
            batch = await events.wait(
                run_id, after=cursor, limit=100, timeout_ms=settings._XREAD_BLOCK_MS
            )
        except _redis_exceptions.TimeoutError:
            # Benign: the block window elapsed with no new events — semantically
            # identical to an empty result. (redis-py 8.0 defaults socket_timeout
            # to 5s; if it ever equals _XREAD_BLOCK_MS the read raises here on
            # every idle window instead of returning nil. We size socket_timeout
            # well above the block in core/infra/redis.py, but treat the timeout
            # as "no events" regardless so a long, quiet run never spams logs.)
            batch = []
        except Exception as exc:
            logger.warning("chat_run_xread_failed", run_id=run_id, error=str(exc), exc_info=exc)
            await asyncio.sleep(0.5)
            batch = []

        if not batch:
            # Nothing arrived: check whether the run reached a terminal state
            current = runtime.get_run(run_id)
            if current is not None and current.status in settings._TERMINAL_STATUSES:
                # After the terminal state, read the latest events once more (covers the race)
                try:
                    tail = await events.read(run_id, after=cursor, limit=200)
                except Exception:
                    tail = []
                delivered, cursor, _terminal = followups._drain(tail, from_offset, cursor)
                for event in delivered:
                    yield event
                return
            continue

        delivered, cursor, terminal_seen = followups._drain(batch, from_offset, cursor)
        for event in delivered:
            yield event
        if terminal_seen:
            return
