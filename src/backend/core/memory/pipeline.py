"""Post-response memory pipeline — all write operations are stripped out of the SSE main path into here.

Core contract:
1. `schedule_post_response_tasks()` is a sync function; it only does `asyncio.create_task()` and **never awaits**
2. A global `asyncio.Semaphore` bounds concurrency to prevent task pile-up
3. Internal exceptions are swallowed by try/except and never bubble up
4. Milvus circuit breaker: after N consecutive failures it short-circuits for a while, skipping directly during that window

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
from core.infra.rate_limit import CircuitBreaker as _InfraCircuitBreaker, CircuitBreakerState
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
    """Fire-and-forget scheduling of post-response memory tasks.

    **Must be a sync function**; callers do not await. SSE has already closed and
    the user is no longer waiting.

    Four gates (skip if any fails — defensive checks so nothing is mistakenly
    written even if an upper layer forgets):
    1. `settings.memory.layered_enabled`: master switch for the layered architecture (deployment-level)
    2. `settings.memory.enabled`: global mem0 switch (deployment-level)
    3. `ctx.write_enabled`: **whether the user explicitly consented to writes** (user-level, default False)
    4. `user_message / assistant_message / user_id` are non-empty
    """
    if not settings.memory.layered_enabled:
        return
    if not settings.memory.enabled:
        return
    if not ctx.write_enabled:
        logger.debug("[memory_pipeline] skip: user write_enabled=False (user=%s)", ctx.user_id)
        return
    if not (user_message and assistant_message and ctx.user_id):
        return

    try:
        asyncio.create_task(_run_post_response_safe(ctx, user_message, assistant_message))
    except RuntimeError:
        logger.warning("[memory_pipeline] no running loop, skipping post-response task")


async def _run_post_response_safe(
    ctx: MemoryContext,
    user_message: str,
    assistant_message: str,
) -> None:
    """Body of the post-response task. Swallows all exceptions; bounded concurrency."""
    try:
        sem = get_background_semaphore()
    except Exception as exc:
        logger.warning("[memory_pipeline] semaphore unavailable: %s", exc)
        return

    async with sem:
        try:
            await _run_pipeline(ctx, user_message, assistant_message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[memory_pipeline] post-response task failed")


async def _run_pipeline(
    ctx: MemoryContext,
    user_message: str,
    assistant_message: str,
) -> None:
    """The actual pipeline: classify → extract → sanitize → write to the matching layer → audit.

    Extractors live in `core.memory.extractors.router`; the import is placed here
    to avoid a circular dependency.
    """
    from core.memory.extractors.router import classify_conversation, run_extractors_with_timeout

    classes = classify_conversation(user_message, assistant_message)
    if not classes:
        logger.debug("[memory_pipeline] empty class set, skipping")
        return

    logger.info(
        "[memory_pipeline] user=%s workspace=%s classes=%s",
        ctx.user_id, ctx.workspace_id, sorted(c.value for c in classes),
    )

    results = await run_extractors_with_timeout(
        classes=classes,
        user_message=user_message,
        assistant_message=assistant_message,
        ctx=ctx,
        timeout_s=settings.memory.extract_timeout_s,
    )

    # results has the shape { ExtractorType: list[dict] | dict };
    # the actual persistence logic is handled by each layer writer
    from core.memory.extractors.writers import write_layered
    await write_layered(results, ctx)
