"""Bounded command wake events, separate from per-run output projections."""

import asyncio
import hashlib
import threading
import time
from collections import deque

from core.infra.redis import get_redis, redis_configured

TTL_SECONDS = 3600
MAX_EVENTS = 512


def topic(user_id, chat_id):
    digest = hashlib.sha256((user_id + "\0" + chat_id).encode()).hexdigest()
    return "jx:command_wakes:" + digest


class MemoryCommandEvents:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = {}
        self.waiters = {}
        self.sequence = 0

    def _log(self, key):
        now = time.monotonic()
        for stale, (expires, _) in list(self.logs.items()):
            if expires <= now:
                self.logs.pop(stale)
        return self.logs.get(key, (0, deque(maxlen=MAX_EVENTS)))[1]

    async def tail(self, user_id, chat_id):
        with self.lock:
            log = self._log(topic(user_id, chat_id))
            return log[-1][0] if log else 0

    async def publish(self, user_id, chat_id, run_id):
        key = topic(user_id, chat_id)
        with self.lock:
            log = self._log(key)
            self.sequence += 1
            log.append((self.sequence, run_id))
            self.logs[key] = (time.monotonic() + TTL_SECONDS, log)
            waiters = list(self.waiters.get(key, ()))
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(_wake, future)
            except RuntimeError:
                if not loop.is_closed():
                    raise
                # An abruptly closed subscriber cannot receive future events.
                with self.lock:
                    subscribers = self.waiters.get(key)
                    if subscribers is not None:
                        subscribers.discard((loop, future))
                        if not subscribers:
                            self.waiters.pop(key)

    async def wait(self, user_id, chat_id, cursor, timeout=15):
        key = topic(user_id, chat_id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = (loop, future)
        with self.lock:
            log = self._log(key)
            batch = [item for item in log if item[0] > cursor]
            if batch:
                # Any trimming is repaired from the authoritative DB snapshot.
                return batch[-1][0], [run for _, run in batch], len(log) == MAX_EVENTS
            self.waiters.setdefault(key, set()).add(entry)
        try:
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return cursor, [], False
        finally:
            with self.lock:
                subscribers = self.waiters.get(key)
                if subscribers is not None:
                    subscribers.discard(entry)
                    if not subscribers:
                        self.waiters.pop(key)
        with self.lock:
            log = self._log(key)
            batch = [item for item in log if item[0] > cursor]
            return (
                batch[-1][0] if batch else cursor,
                [run for _, run in batch],
                len(log) == MAX_EVENTS,
            )


def _wake(future):
    if not future.done():
        future.set_result(None)


class RedisCommandEvents:
    async def tail(self, user_id, chat_id):
        rows = await get_redis().xrevrange(topic(user_id, chat_id), count=1)
        return rows[0][0] if rows else "0-0"

    async def publish(self, user_id, chat_id, run_id):
        key = topic(user_id, chat_id)
        async with get_redis().pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"run_id": run_id}, maxlen=MAX_EVENTS, approximate=False)
            pipe.expire(key, TTL_SECONDS)
            await pipe.execute()

    async def wait(self, user_id, chat_id, cursor, timeout=15):
        key = topic(user_id, chat_id)
        rows = await get_redis(blocking=True).xread(
            {key: cursor}, count=MAX_EVENTS, block=max(1, int(timeout * 1000))
        )
        if not rows:
            return cursor, [], False
        batch = rows[0][1]
        # A saturated stream can have evicted the captured prefix.
        size = await get_redis().xlen(key)
        return batch[-1][0], [fields["run_id"] for _, fields in batch], size >= MAX_EVENTS


_memory = MemoryCommandEvents()
_redis = RedisCommandEvents()


def get_command_events():
    if redis_configured():
        return _redis
    from core.infra.worker_count import resolve

    if resolve() != 1:
        raise RuntimeError("In-memory command events require a single worker")
    return _memory
