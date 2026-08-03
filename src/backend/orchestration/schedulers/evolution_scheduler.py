"""Nightly backstop for the evolution cycle.

The per-turn trigger handles the common case, but it only fires while people are
talking to the system. A quiet week would leave accumulated evidence unmined,
and a burst that ends just under the threshold would sit there indefinitely. So
a daily pass runs regardless.

Built on the shared worker base, which means it inherits the Redis day-lock (one
instance per day across replicas) and the same cost budget as distillation —
rather than a second, independently-tuned ledger nobody can reconcile.
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

_REDIS_LOCK_PREFIX = "evolution:cycle:"
_LOCK_TTL_S = 3600

# Runs after the distillation window so a night's distilled drafts are already
# in place when the cycle looks for patterns.
CRON_EXPRESSION = os.getenv("EVOLUTION_CRON", "45 3 * * *")
CRON_TIMEZONE = os.getenv("EVOLUTION_CRON_TZ", "Asia/Shanghai")
ENABLED = (os.getenv("EVOLUTION_SCHEDULER_ENABLED", "true") or "").lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# How far back the nightly harvest looks. Wide by default because the whole
# argument for a backfill is that it makes the evidence corpus large enough to
# mine on day one instead of after weeks of waiting — and the support threshold
# stays where it is, so a bigger corpus is the only honest way to reach it.
BACKFILL_DAYS = int(os.getenv("EVOLUTION_BACKFILL_DAYS", "180") or 180)
# Bounded per night so a first pass over a long history cannot monopolise the
# database. Already-harvested turns are skipped, so it resumes rather than repeats.
BACKFILL_MAX_BATCHES = int(os.getenv("EVOLUTION_BACKFILL_MAX_BATCHES", "25") or 25)

_instance: Optional["EvolutionCronScheduler"] = None


def get_scheduler() -> Optional["EvolutionCronScheduler"]:
    return _instance


class EvolutionCronScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running or not ENABLED:
            logger.info("[evolution-cron] disabled")
            return
        global _instance
        _instance = self
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[evolution-cron] started (%s %s)", CRON_EXPRESSION, CRON_TIMEZONE)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                sleep_s = seconds_until_next_cron(CRON_EXPRESSION, CRON_TIMEZONE)
                logger.info("[evolution-cron] next fire in %.0f s", sleep_s)
                await interruptible_sleep(sleep_s, lambda: self._running)
                if not self._running:
                    break
                await self._fire_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[evolution-cron] loop error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    async def _fire_once(self) -> None:
        acquired = await acquire_day_lock(
            _REDIS_LOCK_PREFIX,
            timezone=CRON_TIMEZONE,
            ttl_s=_LOCK_TTL_S,
            log_tag="evolution-cron",
        )
        if not acquired:
            return

        from core.evolution.backfill import backfill_episodes
        from core.evolution.loop import run_evolution_cycle

        loop = asyncio.get_running_loop()

        # Harvest first, mine second. Two reasons this is not optional:
        #
        # 1. Live assembly runs in a post-response background task, so any turn
        #    whose assembly failed (restart mid-flight, transient DB error) has
        #    no Episode and is invisible to mining forever. This is the
        #    self-healing pass for those.
        # 2. Nothing else calls the backfill. It was written to unlock history on
        #    day one and had no caller, so the corpus only ever held whatever a
        #    manual invocation happened to produce — which is the real reason
        #    mining found nothing, not the support threshold.
        #
        # Bounded by batch count rather than left to run to completion: a first
        # pass over a long history must not hold the database for the whole
        # night. It resumes on the next run, because turns that already have an
        # Episode are filtered out.
        try:
            stats = await loop.run_in_executor(
                None,
                lambda: backfill_episodes(
                    days=BACKFILL_DAYS, batch_size=200, max_batches=BACKFILL_MAX_BATCHES
                ),
            )
            logger.info(
                "[evolution-cron] backfill: scanned=%s created=%s skipped=%s",
                stats.get("scanned"),
                stats.get("created"),
                stats.get("skipped"),
            )
        except Exception as exc:  # noqa: BLE001
            # A failed harvest must not stop the mining pass over what is
            # already there.
            logger.warning("[evolution-cron] backfill failed: %s", exc)

        # The nightly pass ignores the debounce on purpose: it is the backstop
        # for evidence the per-turn trigger never got to.
        report = await loop.run_in_executor(None, lambda: run_evolution_cycle(limit=800))
        logger.info(
            "[evolution-cron] done: episodes=%s patterns=%s candidates=%s",
            report.get("episodes"),
            report.get("patterns"),
            len(report.get("candidates") or []),
        )
