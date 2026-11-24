"""Per-(event loop, session) asyncio locks shared by the cloud sandbox providers."""

from __future__ import annotations

import asyncio
import threading
from weakref import WeakValueDictionary


class SessionLockMixin:
    """Hand out one ``asyncio.Lock`` per session **per event loop**.

    Providers are process-wide singletons, but sub-agents run their own event
    loop in a separate thread. An ``asyncio.Lock`` binds to the loop that first
    awaits it and raises "is bound to a different event loop" on any other, so
    a single lock per session would break every caller but the first loop.

    Entries are weak: a lock nobody holds or awaits disappears on its own. The
    registry lock only guards the dict (no ``await`` inside), so a thread lock
    works across loops where an ``asyncio.Lock`` would reintroduce the bug.
    """

    def _init_session_locks(self) -> None:
        self._session_locks: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()
        self._registry_lock = threading.Lock()

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        key = (id(asyncio.get_running_loop()), session_id)
        with self._registry_lock:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[key] = lock
            return lock
