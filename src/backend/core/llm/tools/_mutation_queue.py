"""Serialize Edit/Write calls that target the same file (pi-mono ``file-mutation-queue.ts``).

Parallel tool calls on one file would otherwise read the same base content and the last
write would silently drop the other changes. Different files still run in parallel.
"""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Weak values: a lock disappears once no call holds or waits on it.
_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


@asynccontextmanager
async def file_mutation_lock(key: str) -> AsyncIterator[None]:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        yield
