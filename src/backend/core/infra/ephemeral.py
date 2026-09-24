"""Short-lived shared state, independent of any particular store.

Six purpose-named operations cover every non-stream use the backend has for a
TTL keyspace: cached results, one-shot handoffs, rolling counters and mutexes.
Two backends implement them — Redis wherever one is configured, an in-process
map wherever none is (the desktop's single-process backend).

The seam is deliberately the *purpose*, not a Redis command surface: nothing
here emulates the wire protocol, so no client/server version pairing can break
it the way an in-process Redis imitation can.

Every entry carries a TTL by construction, so the in-process backend loses
nothing a restart would not have lost anyway — that backend and the process
that owns it die together.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Protocol, runtime_checkable

from redis.exceptions import WatchError

from core.infra.redis import get_redis, redis_configured


def _seconds(ttl: int) -> int:
    """Validate a TTL so both backends agree on what they reject.

    Redis refuses a non-positive expiry outright; an in-process map would
    happily store an already-dead entry. Checking here keeps the two from
    diverging on an input a caller should not have produced anyway.
    """
    ttl = int(ttl)
    if ttl <= 0:
        raise ValueError(f"ttl must be a positive number of seconds, got {ttl}")
    return ttl


@runtime_checkable
class EphemeralState(Protocol):
    """A TTL keyspace, addressed by what callers actually do with it.

    Every write takes a positive ``ttl`` in seconds; a non-positive one is a
    caller error and raises ``ValueError`` on either backend.
    """

    async def keys(self, prefix: str) -> list[str]:
        """List live keys under a namespace."""

    async def compare_exchange(self, key: str, expected: str, value: str, *, ttl: int) -> bool:
        """Replace an existing value only if it still equals expected."""

    async def get(self, key: str) -> Optional[str]:
        """Read a value, or None when absent/expired."""

    async def put(self, key: str, value: str, *, ttl: int) -> None:
        """Write a value that expires after *ttl* seconds."""

    async def take(self, key: str) -> Optional[str]:
        """Read and remove in one step (one-shot handoffs)."""

    async def drop(self, *keys: str) -> None:
        """Remove keys; missing ones are not an error."""

    async def bump(self, key: str, amount: float = 1.0, *, ttl: int) -> float:
        """Add to a counter (negative to subtract) and return the new total."""

    async def claim(self, key: str, *, ttl: int) -> bool:
        """Take a mutex; True only for the caller that created the entry."""

    async def hold(self, key: str, token: str, *, ttl: int) -> bool:
        """Take or extend a mutex owned by *token*; True while it is ours.

        ``claim`` can only take a mutex, never extend one, so a holder that must
        keep a lock alive across many TTLs has no way to say "still me".
        Renewing through ``put`` would instead let a holder whose lease already
        lapsed overwrite whoever legitimately took it. ``hold`` expresses both
        steps against one ownership token, which is what a leader lease needs.
        """


class RedisEphemeralState:
    """Redis-backed implementation for deployments that have one."""

    async def keys(self, prefix: str) -> list[str]:
        return [key async for key in get_redis().scan_iter(match=prefix + "*", count=128)]

    async def compare_exchange(self, key: str, expected: str, value: str, *, ttl: int) -> bool:
        async with get_redis().pipeline() as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != expected:
                    return False
                pipe.multi()
                pipe.set(key, value, ex=_seconds(ttl))
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def get(self, key: str) -> Optional[str]:
        return await get_redis().get(key)

    async def put(self, key: str, value: str, *, ttl: int) -> None:
        await get_redis().set(key, value, ex=_seconds(ttl))

    async def take(self, key: str) -> Optional[str]:
        redis = get_redis()
        try:
            return await redis.getdel(key)
        except AttributeError:  # pragma: no cover - pre-GETDEL clients
            value = await redis.get(key)
            if value is not None:
                await redis.delete(key)
            return value

    async def drop(self, *keys: str) -> None:
        if keys:
            await get_redis().delete(*keys)

    async def bump(self, key: str, amount: float = 1.0, *, ttl: int) -> float:
        redis = get_redis()
        total = await redis.incrbyfloat(key, amount)
        await redis.expire(key, _seconds(ttl))
        return float(total)

    async def claim(self, key: str, *, ttl: int) -> bool:
        return bool(await get_redis().set(key, "1", ex=_seconds(ttl), nx=True))

    async def hold(self, key: str, token: str, *, ttl: int) -> bool:
        ttl_ms = _seconds(ttl) * 1000
        redis = get_redis()
        if await redis.set(key, token, px=ttl_ms, nx=True):
            return True
        # Extending has to be atomic with the check that we still own it.
        # Reading the owner and then extending as two plain commands leaves a
        # window where the lease lapses in between, and the extend would
        # resurrect a lock somebody else now holds — two leaders, the one
        # outcome this must never produce. WATCH makes EXEC fail if the key
        # moved, which is the same answer as "not ours".
        #
        # A Lua script would say this in one round trip, but the test double
        # this project pins its Redis behaviour against (see the fakeredis note
        # in requirements.txt) does not run Lua, and an operation that only the
        # in-process backend can be tested on is worse than one extra round
        # trip on a call that happens a few times a minute.
        async with redis.pipeline() as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != token:
                    return False
                pipe.multi()
                pipe.pexpire(key, ttl_ms)
                await pipe.execute()
                return True
            except WatchError:
                return False


class LocalEphemeralState:
    """In-process implementation for deployments without Redis.

    Guarded by a plain :class:`threading.Lock` rather than an asyncio one: the
    backend runs sub-agents on dedicated thread-local event loops, and an
    asyncio primitive created on one loop cannot be awaited from another. The
    lock is never held across an await, so it can never block a loop.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    async def keys(self, prefix: str) -> list[str]:
        with self._lock:
            self._prune(time.time())
            return [key for key in self._entries if key.startswith(prefix)]

    async def compare_exchange(self, key: str, expected: str, value: str, *, ttl: int) -> bool:
        with self._lock:
            now = time.time()
            self._prune(now)
            entry = self._entries.get(key)
            if entry is None or entry[1] != expected:
                return False
            self._entries[key] = (now + _seconds(ttl), value)
            return True

    def _read(self, key: str, *, pop: bool = False) -> Optional[str]:
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            if pop:
                self._entries.pop(key, None)
            return value

    def _prune(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._entries.items() if exp <= now]
        for key in expired:
            self._entries.pop(key, None)

    async def get(self, key: str) -> Optional[str]:
        return self._read(key)

    async def put(self, key: str, value: str, *, ttl: int) -> None:
        expires_at = time.time() + _seconds(ttl)
        with self._lock:
            self._prune(expires_at - ttl)
            self._entries[key] = (expires_at, value)

    async def take(self, key: str) -> Optional[str]:
        return self._read(key, pop=True)

    async def drop(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    async def bump(self, key: str, amount: float = 1.0, *, ttl: int) -> float:
        ttl = _seconds(ttl)
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(key)
            current = float(entry[1]) if entry is not None else 0.0
            total = current + amount
            self._entries[key] = (now + ttl, repr(total))
            return total

    async def claim(self, key: str, *, ttl: int) -> bool:
        ttl = _seconds(ttl)
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._entries:
                return False
            self._entries[key] = (now + ttl, "1")
            return True

    async def hold(self, key: str, token: str, *, ttl: int) -> bool:
        ttl = _seconds(ttl)
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(key)
            if entry is not None and entry[1] != token:
                return False
            self._entries[key] = (now + ttl, token)
            return True


_REDIS_STATE = RedisEphemeralState()
_LOCAL_STATE = LocalEphemeralState()


def get_ephemeral_state() -> EphemeralState:
    """The short-lived-state store this deployment should use."""
    return _REDIS_STATE if redis_configured() else _LOCAL_STATE


__all__ = [
    "EphemeralState",
    "LocalEphemeralState",
    "RedisEphemeralState",
    "get_ephemeral_state",
]
