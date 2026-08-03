"""Daily memory-TTL sweep scheduler.

Runs `core.memory.ttl_sweeper.sweep_expired_memories` once a day. Expired
entries are already hidden from retrieval by mem0's native expiration_date, so
this pass is pure hygiene — it keeps Milvus from accumulating rows nobody can
recall. Built on the shared worker base for the Redis day-lock (one instance per
day across replicas), same as the evolution scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from orchestration.schedulers._worker_base import (
    acquire_day_lock,
    interruptible_sleep,
    seconds_until_next_cron,
)

logger = logging.getLogger(__name__)

_REDIS_LOCK_PREFIX = "memory:ttl_sweep:"
_LOCK_TTL_S = 3600

# After the evolution window (03:45) so the two nightly passes don't contend.
CRON_EXPRESSION = os.getenv("MEMORY_TTL_SWEEP_CRON", "15 4 * * *")
CRON_TIMEZONE = os.getenv("MEMORY_TTL_SWEEP_TZ", "Asia/Shanghai")
ENABLED = (os.getenv("MEMORY_TTL_SWEEP_ENABLED", "true") or "").lower() not in (
    "0",
    "false",
    "no",
    "off",
)

_instance: Optional["MemoryTtlScheduler"] = None


def get_scheduler() -> Optional["MemoryTtlScheduler"]:
    return _instance


class MemoryTtlScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running or not ENABLED:
            logger.info("[memory-ttl-cron] disabled")
            return
        global _instance
        _instance = self
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[memory-ttl-cron] started (%s %s)", CRON_EXPRESSION, CRON_TIMEZONE)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                sleep_s = seconds_until_next_cron(CRON_EXPRESSION, CRON_TIMEZONE)
                logger.info("[memory-ttl-cron] next fire in %.0f s", sleep_s)
                await interruptible_sleep(sleep_s, lambda: self._running)
                if not self._running:
                    break
                await self._fire_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[memory-ttl-cron] loop error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    async def _fire_once(self) -> None:
        acquired = await acquire_day_lock(
            _REDIS_LOCK_PREFIX,
            timezone=CRON_TIMEZONE,
            ttl_s=_LOCK_TTL_S,
            log_tag="memory-ttl-cron",
        )
        if not acquired:
            return

        from core.memory.ttl_sweeper import sweep_expired_memories

        stats = await sweep_expired_memories()
        logger.info(
            "[memory-ttl-cron] done: scanned=%s expired=%s deleted=%s",
            stats.get("scanned"), stats.get("expired"), stats.get("deleted"),
        )
