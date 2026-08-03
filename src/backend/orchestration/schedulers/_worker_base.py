"""Shared primitives for background queue workers (GCE ticket 02).

The skill-distillation scheduler already proved out the pattern every other
background pipeline in this project needs: a Redis day-lock so only one instance
fires per day, ``FOR UPDATE SKIP LOCKED`` claiming so concurrent workers never
grab the same row, a bounded drain, and a cost budget that can hard-stop the
whole thing.

The evolution pipeline (Episode assembly, credit assignment, replay evaluation)
needs exactly those four.  Re-implementing them would produce two independent
cost ledgers — and then nobody could answer "what did the system spend today",
which is precisely the question that matters when replay costs can rival the
product's own inference bill.  So the primitives live here and both pipelines
share one ledger.

This module is deliberately storage-shaped rather than domain-shaped: it knows
about "a table with a status column" and nothing about skills or candidates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

import pytz

from core.db.engine import SessionLocal
from core.infra.redis import get_redis

logger = logging.getLogger(__name__)

# A day-lock is held long enough that a crashed instance cannot wedge the next
# run forever, but long enough that a slow legitimate drain keeps its claim.
DEFAULT_DAY_LOCK_TTL_S = 3600


async def acquire_day_lock(
    prefix: str,
    *,
    timezone: str = "Asia/Shanghai",
    ttl_s: int = DEFAULT_DAY_LOCK_TTL_S,
    today: Optional[str] = None,
    log_tag: str = "worker",
) -> bool:
    """Try to become *the* instance running today's cycle.

    Returns ``False`` both when another instance holds the lock and when Redis
    is unreachable.  Failing closed is deliberate: a background cycle that
    silently runs twice is worse than one that skips a day, because duplicate
    candidate generation pollutes the evidence chain.
    """
    day = today or datetime.now(pytz.timezone(timezone)).strftime("%Y%m%d")
    lock_key = f"{prefix}{day}"
    try:
        redis = get_redis()
        acquired = await redis.set(lock_key, "1", ex=ttl_s, nx=True)
    except Exception as exc:
        logger.warning("[%s] redis lock error, skipping fire (%s)", log_tag, exc)
        return False
    if not acquired:
        logger.info("[%s] another instance already holding today's lock", log_tag)
        return False
    logger.info("[%s] acquired lock %s", log_tag, lock_key)
    return True


def claim_next(
    model: Any,
    *,
    pk_field: str,
    status_field: str = "status",
    queued_value: str = "queued",
    running_value: str = "running",
    order_field: str = "created_at",
    started_field: Optional[str] = "started_at",
    extra_filters: Sequence[Any] = (),
) -> Optional[str]:
    """Atomically flip one queued row to running and return its primary key.

    ``FOR UPDATE SKIP LOCKED`` is what makes concurrent workers safe: each one
    skips rows another worker already holds instead of blocking on them.  The
    row lock is released at commit, so a long LLM call afterwards never holds a
    database row.
    """
    with SessionLocal() as db:
        query = db.query(model).filter(getattr(model, status_field) == queued_value)
        for condition in extra_filters:
            query = query.filter(condition)
        row = (
            query.order_by(getattr(model, order_field).asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if row is None:
            return None
        setattr(row, status_field, running_value)
        if started_field and hasattr(row, started_field):
            setattr(row, started_field, datetime.utcnow())
        db.commit()
        return getattr(row, pk_field)


def recover_stale(
    model: Any,
    *,
    status_field: str = "status",
    running_value: str = "running",
    queued_value: str = "queued",
    started_field: str = "started_at",
    timeout_minutes: int = 60,
    log_tag: str = "worker",
) -> int:
    """Return rows stuck in *running* back to the queue.

    A process that dies mid-item leaves its row claimed forever; without this,
    every restart would leak one slot.  Requeueing rather than failing them is
    the right default because the work is idempotent by construction — that is a
    precondition of using this base class, not an assumption about the caller.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
    try:
        with SessionLocal() as db:
            stale = (
                db.query(model)
                .filter(
                    getattr(model, status_field) == running_value,
                    getattr(model, started_field) < cutoff,
                )
                .all()
            )
            for row in stale:
                setattr(row, status_field, queued_value)
            if stale:
                db.commit()
                logger.info("[%s] requeued %d stale running rows", log_tag, len(stale))
            return len(stale)
    except Exception as exc:
        logger.warning("[%s] stale recovery failed: %s", log_tag, exc)
        return 0


async def drain_queue(
    *,
    claim: Callable[[], Optional[str]],
    process: Callable[[str], Awaitable[None]],
    concurrency: int,
    semaphore: Optional[asyncio.Semaphore] = None,
    budget_check: Optional[Callable[[], Awaitable[Tuple[bool, Optional[str]]]]] = None,
    on_budget_exhausted: Optional[Callable[[str, Optional[str]], None]] = None,
    log_tag: str = "worker",
) -> int:
    """Run *concurrency* workers, each claiming until the queue is empty.

    ``budget_check`` is consulted per item, after the claim.  When it says stop,
    the already-claimed item is handed to ``on_budget_exhausted`` so the caller
    can mark it (rather than leaving it stranded in *running*) and the whole
    drain unwinds — a budget is a hard ceiling, not a per-item hint.
    """
    sem = semaphore or asyncio.Semaphore(max(1, concurrency))

    async def _worker() -> int:
        processed = 0
        while True:
            async with sem:
                item_id = claim()
                if item_id is None:
                    return processed

                if budget_check is not None:
                    allowed, reason = await budget_check()
                    if not allowed:
                        logger.info("[%s] budget exhausted: %s", log_tag, reason)
                        if on_budget_exhausted is not None:
                            on_budget_exhausted(item_id, reason)
                        return processed

                try:
                    await process(item_id)
                except Exception as exc:
                    # One poisoned item must not take down the drain; the item's
                    # own handler is responsible for recording its failure.
                    logger.error(
                        "[%s] item %s failed: %s", log_tag, item_id, exc, exc_info=exc
                    )
                processed += 1

    results: List[Any] = await asyncio.gather(
        *(_worker() for _ in range(max(1, concurrency))), return_exceptions=True
    )
    total = 0
    for result in results:
        if isinstance(result, Exception):
            logger.error("[%s] worker crashed: %s", log_tag, result, exc_info=result)
        else:
            total += result
    return total


def seconds_until_next_cron(expression: str, timezone: str) -> float:
    """Seconds until the next cron fire, floored at 1s so we never busy-spin."""
    from croniter import croniter

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    next_fire = croniter(expression, now).get_next(datetime)
    return max(1.0, (next_fire - now).total_seconds())


async def interruptible_sleep(seconds: float, is_running: Callable[[], bool]) -> None:
    """Sleep in chunks so a shutdown signal is honoured within a minute.

    A scheduler that sleeps until 2am cannot be stopped at 3pm without this.
    """
    remaining = seconds
    while remaining > 0 and is_running():
        chunk = min(remaining, 60.0)
        await asyncio.sleep(chunk)
        remaining -= chunk
