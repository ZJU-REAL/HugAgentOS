"""Append ordered run events and persist terminal stream markers."""

from __future__ import annotations

import asyncio
import json
import time
import typing as _typing
from typing import Any, Dict

import core.runs.events.store as store
import core.runs.ownership as ownership
import core.runs.settings as settings
import core.runs.journal as run_journal
from core.infra.logging import get_logger
from core.runs.events.store import RunEventStream
from core.runs.journal import RunJournal

logger = get_logger(__name__)


def _events() -> RunEventStream:
    """This deployment's run event log (Redis Streams, or in-process)."""
    return store.get_run_event_stream()


class EventOffsets:
    """Durable SSE offsets for one worker, without a database write per event.

    Allocating an offset and proving the worker still owns the run used to be
    the same write, so every token cost a serialized transaction — invisible
    against a server database, and a disk flush per token on the desktop, where
    the journal is SQLite and the person is watching the answer appear.

    The two are separable. Offsets come from reservations the journal hands out
    in runs, and this keeps the unused remainder. Ownership is proven by reading
    the lease rather than writing to it, and on a bounded schedule rather than
    once per event: measured in the deployment that read costs ~0.5 ms, which at
    forty concurrent streams is a whole core spent asking a question whose
    answer almost never changes.

    Ownership can stop being ours two ways, and the bound is sized against the
    slower one:

    * This process notices itself — its own heartbeat fails to renew, or the
      user cancelled here. ``lease_lost`` is set, and the very next event is
      fenced without leaving the loop at all, so this path got *faster*, not
      looser.
    * Another worker decided it — a cancel that landed on a different process,
      or a recovery that re-claimed this run. That can only arrive through the
      shared journal, so the read cannot be dropped, only bounded: at most one
      per ``_CANCEL_VISIBILITY_SEC``, the same cadence the chunk loop already
      polls cancellation on. Re-claiming requires the lease to have *expired*,
      which means this worker went a full lease without renewing — it was not
      emitting during that stretch either — so the window where a fenced worker
      could still append is bounded by a quarter second and reached only by a
      process that had stopped producing anyway.

    A terminal event is allocated on its own: it has to sit after every live
    offset, including the ones a reservation left unused. That allocation is an
    owned write, so a run always fences hard on the way out.
    """

    def __init__(
        self,
        journal: RunJournal,
        run_id: str,
        owner: str,
        *,
        lease_lost: asyncio.Event,
    ):
        self._journal = journal
        self._run_id = run_id
        self._owner = owner
        self._next = 0
        self._remaining = 0
        self._lease_lost = lease_lost
        self._proven_at = 0.0
        # The offset most recently handed out. Callers persist it with their
        # checkpoints so a reconnecting follower knows where the run had got to.
        self.last = 0

    async def take(self, *, terminal: bool = False) -> int:
        if terminal:
            self._remaining = 0
            self.last = await asyncio.to_thread(
                self._journal.allocate_event_offset, self._run_id, terminal=True
            )
            return self.last
        if self._remaining == 0:
            # A reservation is an owned write: it fences on its own, and more
            # strongly than a read does.
            self._next = await asyncio.to_thread(self._reserve)
            self._remaining = settings._OFFSET_RESERVATION
            self._proven_at = time.monotonic()
        else:
            await self._prove_ownership()
        offset = self._next
        self._next += 1
        self._remaining -= 1
        self.last = offset
        return offset

    async def _prove_ownership(self) -> None:
        """Raise ``RunLeaseLost`` unless this worker still owns the run."""
        if self._lease_lost.is_set():
            # Our own heartbeat already decided this; the journal cannot
            # disagree, so there is nothing to go and ask.
            raise run_journal.RunLeaseLost(self._run_id)
        now = time.monotonic()
        if now - self._proven_at < settings._CANCEL_VISIBILITY_SEC:
            return
        await asyncio.to_thread(self._journal.require_lease, self._run_id, self._owner)
        self._proven_at = now

    async def emit(self, event: Dict[str, Any], *, terminal: bool = False) -> None:
        """Allocate this event's offset and append it to the run's log."""
        await self.take(terminal=terminal)
        await xadd_event(self._run_id, self.last, event)

    def _reserve(self) -> int:
        return self._journal.allocate_event_offset(
            self._run_id, owner=self._owner, count=settings._OFFSET_RESERVATION
        )


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


async def _write_terminal_to_stream(
    run_id: str,
    *,
    chat_id: str,
    error_text: str,
    cancelled: bool = False,
) -> None:
    """Write an error event + terminal marker + EXPIRE.

    Used when the worker isn't around to emit them itself (cross-process cancel,
    orphan recovery). Failure is logged but not raised.
    """
    err_event: _typing.Dict[str, _typing.Any] = {
        "type": "error",
        "error": error_text,
        "delta": error_text,
        "chat_id": chat_id,
    }
    term_event: _typing.Dict[str, _typing.Any] = {
        "type": settings.TERMINAL_TYPE,
        "chat_id": chat_id,
    }
    if cancelled:
        err_event["_cancelled"] = True
        term_event["_cancelled"] = True
    try:
        journal = ownership.journal()
        error_offset = journal.allocate_event_offset(run_id, terminal=True)
        await xadd_event(run_id, error_offset, err_event)
        terminal_offset = journal.allocate_event_offset(run_id, terminal=True)
        await xadd_event(run_id, terminal_offset, term_event)
        await expire_stream(run_id)
    except Exception as exc:
        logger.warning("chat_run_terminal_write_failed", run_id=run_id, error=str(exc))


async def xadd_event(run_id: str, offset: int, event: Dict[str, Any]) -> None:
    """Stamp an SSE event with its offset and wall clock, append it to the run's log.

    ``server_ts`` is when the server produced the event. A client that joins
    late — after a reconnect, a session switch, or from a second device —
    replays the log and reads these stamps, so its "waiting since" clock
    reflects the run's real timeline instead of the moment this browser
    happened to receive them. It deliberately has no underscore prefix: that
    prefix marks transport-internal keys, which ``follow_run_as_sse`` strips
    before the frame goes out, and this one is meant for the client.
    """
    payload = {**event, "_offset": offset, "server_ts": int(time.time() * 1000)}
    try:
        await _events().append(run_id, payload)
    except Exception as exc:
        logger.warning("chat_run_xadd_failed", run_id=run_id, error=str(exc))


async def expire_stream(run_id: str) -> None:
    try:
        await _events().expire(run_id, settings._STREAM_TTL_SECONDS)
    except Exception as exc:
        logger.warning("chat_run_expire_failed", run_id=run_id, error=str(exc))


async def _reset_stream_projection(run_id: str) -> None:
    """Drop a crashed worker's partial projection before replaying its run."""
    try:
        await _events().clear(run_id)
    except Exception as exc:
        logger.warning("chat_run_projection_reset_failed", run_id=run_id, error=str(exc))
