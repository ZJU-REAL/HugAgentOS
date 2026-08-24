"""Post-response memory admission and shared write-path controls.

Core contract:
1. `schedule_post_response_tasks()` commits a durable outbox row before it returns
2. The low-latency path only wakes the lifecycle-owned worker; recovery comes from the DB row
3. A global `asyncio.Semaphore` bounds outbox consumption concurrency
4. Milvus circuit breaker short-circuits an unhealthy external store

Public interface:
- `schedule_post_response_tasks(ctx, user_msg, assistant_msg)` — call after SSE closes
- `milvus_breaker` — global breaker instance; both retrieve and write paths should check it first
- `get_background_semaphore()` — lazily created semaphore (must be created inside an event loop)
"""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Optional

from core.config.settings import settings
from core.infra.rate_limit import CircuitBreaker as _InfraCircuitBreaker
from core.infra.rate_limit import CircuitBreakerState
from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)


# ─── Global semaphore (lazy init) ──────────────────────────────────────────


_bg_semaphore: Optional[asyncio.Semaphore] = None
_sem_lock = Lock()


def get_background_semaphore() -> asyncio.Semaphore:
    """Return the global background-task semaphore. Must be first called inside an event loop."""
    global _bg_semaphore
    with _sem_lock:
        if _bg_semaphore is None:
            _bg_semaphore = asyncio.Semaphore(settings.memory.bg_max_concurrency)
        return _bg_semaphore


# ─── Circuit breaker ───────────────────────────────────────────────────────


class CircuitBreaker(_InfraCircuitBreaker):
    """Reuses `core.infra.rate_limit.CircuitBreaker` (three-state machine),
    additionally exposing `is_open()` plus public `record_success()` / `record_failure()`.

    Why a local subclass is needed: the base class's `.call()` / `.call_async()` are
    "decorator-style" calls that wrap the business function, whereas the memory
    module's callers (retrieve / writers) use a manual "check state first, then decide
    whether to skip" pattern and need a lightweight is_open() interface.
    """

    def is_open(self) -> bool:
        if self._should_attempt_reset():
            self.state = CircuitBreakerState.HALF_OPEN
            self.success_count = 0
        return self.state == CircuitBreakerState.OPEN

    def record_success(self) -> None:
        self._on_success()

    def record_failure(self) -> None:
        self._on_failure()


# Global Milvus circuit breaker (shared by retrieve / write)
milvus_breaker = CircuitBreaker(
    name="memory_milvus",
    failure_threshold=settings.memory.breaker_threshold,
    success_threshold=1,
    timeout=settings.memory.breaker_cooldown_s,
)


# ─── Main entry: post-response task scheduling ─────────────────────────────


def schedule_post_response_tasks(
    ctx: MemoryContext,
    user_message: str,
    assistant_message: str,
) -> None:
    """Durably admit post-response memory work, then kick a worker.

    **Must be a sync function**; callers do not await. The database insert is
    intentionally synchronous so an immediate process exit cannot lose the
    accepted write request.

    Four gates (skip if any fails — defensive checks so nothing is mistakenly
    written even if an upper layer forgets):
    1. `settings.memory.layered_enabled`: master switch for the layered architecture (deployment-level)
    2. `settings.memory.enabled`: global mem0 switch (deployment-level)
    3. `ctx.write_enabled`: **whether the user explicitly consented to writes** (user-level, default False)
    4. `user_message / assistant_message / user_id` are non-empty
    """
    if not settings.memory.layered_enabled:
        _report_settlement(ctx)
        return
    if not settings.memory.enabled:
        _report_settlement(ctx)
        return
    if not ctx.write_enabled:
        logger.debug("[memory_pipeline] skip: user write_enabled=False (user=%s)", ctx.user_id)
        _report_settlement(ctx)
        return
    if not (user_message and assistant_message and ctx.user_id):
        _report_settlement(ctx)
        return

    try:
        from core.memory.outbox import enqueue_pipeline_job

        enqueue_pipeline_job(ctx, user_message, assistant_message)
    except Exception:
        logger.exception("[memory_pipeline] durable outbox admission failed")
        _report_settlement(ctx, failed=True)
        return

    # Admission has already committed.  A closed event loop or a worker wakeup
    # failure cannot turn a durable request into a failed evolution outcome;
    # the polling worker will recover the row after restart.
    try:
        from core.memory.outbox import kick_outbox_drain

        kick_outbox_drain()
    except Exception:
        logger.debug("[memory_pipeline] outbox wakeup skipped", exc_info=True)


def _report_settlement(
    ctx: MemoryContext,
    items: Optional[list] = None,
    *,
    failed: bool = False,
) -> None:
    """Report the memories this turn actually wrote to its evolution settlement.

    Called on **every** exit path, including the gates that write nothing: the
    turn's card is held open until this arrives, and a silent skip would leave
    it waiting on the watchdog. An empty list is a perfectly good answer — it
    settles the turn to "nothing happened", which draws no card at all.

    Items, not counts: the card lists each memory and offers to edit or delete
    it, which is only possible for entries it can address.
    """
    message_id = getattr(ctx, "message_id", None)
    if not message_id:
        return
    try:
        from core.evolution.settlement_runner import report_memory_writes

        report_memory_writes(message_id, items=list(items or []), failed=failed)
    except Exception as exc:  # noqa: BLE001 - settlement must never break writes
        logger.debug("[memory_pipeline] settlement report skipped: %s", exc)
