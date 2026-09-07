"""The per-run event log behind SSE following and resume.

A chat run writes every SSE event here as it happens; followers replay from a
cursor and then tail for new ones. Six operations cover the whole need:
append, read from a cursor, wait for the next batch, last-write time
(liveness), expire and clear.

Two backends implement it — Redis Streams wherever Redis is configured, an
in-process log wherever none is. Both mint the same ``"{epoch_ms}-{seq}"``
cursor grammar, so callers (and the stale-run reaper, which reads the
millisecond half) never learn which one they are talking to.

On a single-process backend the writer and the reader live in the same
program, so the in-process log needs no serialization, no socket and no
protocol — an ordered buffer plus a wake-up signal.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from core.infra.redis import get_redis, redis_configured

# Cursor meaning "from the very beginning of the log".
START = ""

# Events kept per run. Older ones fall off: a follower that has been away
# longer than this resumes from the run's durable snapshot instead.
MAXLEN = 5000

Entry = Tuple[str, Dict[str, Any]]


def next_cursor(cursor: str) -> str:
    """The smallest cursor strictly greater than *cursor*."""
    if "-" not in cursor:
        return cursor
    stamp, seq = cursor.split("-", 1)
    try:
        return f"{stamp}-{int(seq) + 1}"
    except ValueError:
        return cursor


def cursor_millis(cursor: str) -> Optional[int]:
    """The epoch-millisecond half of a cursor, or None when unparsable."""
    try:
        return int(str(cursor).split("-", 1)[0])
    except (AttributeError, ValueError):
        return None


def _sort_key(cursor: str) -> Tuple[int, int]:
    if not cursor:
        return (-1, -1)
    stamp, _, seq = cursor.partition("-")
    try:
        return (int(stamp), int(seq or 0))
    except ValueError:
        return (-1, -1)


@runtime_checkable
class RunEventStream(Protocol):
    """Append-only event log for one chat run, addressed by cursor."""

    async def append(self, run_id: str, event: Dict[str, Any]) -> None:
        """Add an event to the end of the run's log."""

    async def read(
        self, run_id: str, *, after: str = START, limit: Optional[int] = None
    ) -> List[Entry]:
        """Return events recorded after *after*, oldest first."""

    async def wait(self, run_id: str, *, after: str, limit: int, timeout_ms: int) -> List[Entry]:
        """Block until events appear after *after*, or the timeout elapses."""

    async def last_write_ms(self, run_id: str) -> Optional[int]:
        """Epoch-ms of the newest event, or None when the log is empty."""

    async def expire(self, run_id: str, ttl: int) -> None:
        """Schedule the whole log for removal *ttl* seconds from now."""

    async def clear(self, run_id: str) -> None:
        """Drop the log — used before replaying a crashed run."""


_REDIS_KEY = "jx:chat:run:{run_id}:events"


def redis_stream_key(run_id: str) -> str:
    """Redis key holding a run's event log (Redis backend only)."""
    return _REDIS_KEY.format(run_id=run_id)


class RedisRunEventStream:
    """Redis Streams backend.

    Owns its connection recovery: a failed read can leave the pooled
    connection desynchronised, and every later read on it would fail too, so
    the connection is dropped before the error propagates.
    """

    def _key(self, run_id: str) -> str:
        return redis_stream_key(run_id)

    @staticmethod
    def _decode(entries: Any) -> List[Entry]:
        decoded: List[Entry] = []
        for cursor, fields in entries or ():
            if isinstance(cursor, bytes):
                cursor = cursor.decode()
            raw = fields.get("data") if isinstance(fields, dict) else None
            if raw is None:
                continue
            try:
                decoded.append((str(cursor), json.loads(raw)))
            except (TypeError, ValueError):
                continue
        return decoded

    async def append(self, run_id: str, event: Dict[str, Any]) -> None:
        await get_redis().xadd(
            self._key(run_id),
            {"data": json.dumps(event, ensure_ascii=False)},
            maxlen=MAXLEN,
            approximate=True,
        )

    async def read(
        self, run_id: str, *, after: str = START, limit: Optional[int] = None
    ) -> List[Entry]:
        entries = await get_redis().xrange(
            self._key(run_id),
            min="-" if after == START else next_cursor(after),
            max="+",
            count=limit,
        )
        return self._decode(entries)

    async def wait(self, run_id: str, *, after: str, limit: int, timeout_ms: int) -> List[Entry]:
        redis = get_redis(blocking=True)
        key = self._key(run_id)
        try:
            result = await redis.xread({key: after or "0-0"}, count=limit, block=timeout_ms)
        except Exception:
            await self._drop_connections(redis)
            raise
        for _name, entries in result or ():
            return self._decode(entries)
        return []

    @staticmethod
    async def _drop_connections(redis: Any) -> None:
        try:
            await redis.connection_pool.disconnect(inuse_connections=False)
        except Exception:  # noqa: BLE001 - recovery must not mask the real error
            pass

    async def last_write_ms(self, run_id: str) -> Optional[int]:
        entries = await get_redis().xrevrange(self._key(run_id), max="+", min="-", count=1)
        if not entries:
            return None
        cursor = entries[0][0]
        if isinstance(cursor, bytes):
            cursor = cursor.decode()
        return cursor_millis(cursor)

    async def expire(self, run_id: str, ttl: int) -> None:
        await get_redis().expire(self._key(run_id), ttl)

    async def clear(self, run_id: str) -> None:
        await get_redis().delete(self._key(run_id))


class _RunLog:
    """One run's in-process buffer plus the followers parked on it."""

    __slots__ = ("entries", "expires_at", "last_millis", "sequence", "waiters")

    def __init__(self) -> None:
        self.entries: deque[Entry] = deque(maxlen=MAXLEN)
        self.expires_at: Optional[float] = None
        self.last_millis = 0
        self.sequence = 0
        self.waiters: List[Tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    def mint(self) -> str:
        """Next cursor: monotonic even when the clock repeats or goes back."""
        millis = max(int(time.time() * 1000), self.last_millis)
        self.sequence = self.sequence + 1 if millis == self.last_millis else 0
        self.last_millis = millis
        return f"{millis}-{self.sequence}"

    def since(self, after: str, limit: Optional[int]) -> List[Entry]:
        floor = _sort_key(after)
        found = [entry for entry in self.entries if _sort_key(entry[0]) > floor]
        return found[:limit] if limit else found


class LocalRunEventStream:
    """In-process backend for deployments without Redis.

    Followers may run on a different event loop from the writer (sub-agents
    get their own), so waiters are woken through their own loop rather than
    with an asyncio primitive shared across loops.
    """

    def __init__(self) -> None:
        self._logs: Dict[str, _RunLog] = {}

    def _sweep(self) -> None:
        now = time.time()
        for run_id in [
            run_id
            for run_id, log in self._logs.items()
            if log.expires_at is not None and log.expires_at <= now
        ]:
            self._logs.pop(run_id, None)

    def _log(self, run_id: str, *, create: bool) -> Optional[_RunLog]:
        self._sweep()
        log = self._logs.get(run_id)
        if log is None and create:
            log = self._logs[run_id] = _RunLog()
        return log

    async def append(self, run_id: str, event: Dict[str, Any]) -> None:
        log = self._log(run_id, create=True)
        log.entries.append((log.mint(), event))
        waiters, log.waiters = log.waiters, []
        for loop, ready in waiters:
            try:
                loop.call_soon_threadsafe(ready.set)
            except RuntimeError:  # pragma: no cover - follower's loop is gone
                continue

    async def read(
        self, run_id: str, *, after: str = START, limit: Optional[int] = None
    ) -> List[Entry]:
        log = self._log(run_id, create=False)
        return log.since(after, limit) if log else []

    async def wait(self, run_id: str, *, after: str, limit: int, timeout_ms: int) -> List[Entry]:
        log = self._log(run_id, create=True)
        ready = asyncio.Event()
        waiter = (asyncio.get_running_loop(), ready)
        # Park before looking, so an append landing between the two is not lost.
        log.waiters.append(waiter)
        try:
            found = log.since(after, limit)
            if found:
                return found
            try:
                await asyncio.wait_for(ready.wait(), timeout_ms / 1000)
            except asyncio.TimeoutError:
                return []
            return log.since(after, limit)
        finally:
            if waiter in log.waiters:
                log.waiters.remove(waiter)

    async def last_write_ms(self, run_id: str) -> Optional[int]:
        log = self._log(run_id, create=False)
        if not log or not log.entries:
            return None
        return cursor_millis(log.entries[-1][0])

    async def expire(self, run_id: str, ttl: int) -> None:
        log = self._log(run_id, create=False)
        if log is not None:
            log.expires_at = time.time() + ttl

    async def clear(self, run_id: str) -> None:
        self._logs.pop(run_id, None)


_REDIS_STREAM = RedisRunEventStream()
_LOCAL_STREAM = LocalRunEventStream()


def get_run_event_stream() -> RunEventStream:
    """The run event log this deployment should use."""
    return _REDIS_STREAM if redis_configured() else _LOCAL_STREAM


__all__ = [
    "MAXLEN",
    "START",
    "LocalRunEventStream",
    "RedisRunEventStream",
    "RunEventStream",
    "cursor_millis",
    "get_run_event_stream",
    "next_cursor",
    "redis_stream_key",
]
