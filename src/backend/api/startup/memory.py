"""Application lifecycle operations."""
import os
import sys

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

_persona_distill_worker = None

_memory_outbox_worker = None

async def _startup_evolution_scheduler():
    """Start the nightly evolution-cycle backstop."""
    try:
        from orchestration.workers.evolution_scheduler import EvolutionCronScheduler

        await EvolutionCronScheduler().start()
    except Exception as exc:
        # A scheduler that fails to start must not take the API down with it.
        logger.warning("evolution_scheduler_start_failed", error=str(exc))

async def _startup_memory_ttl_scheduler():
    """Start the daily memory-TTL sweep (deletes expired L2 entries)."""
    if not settings.memory.enabled:
        return
    try:
        from orchestration.workers.memory_ttl_scheduler import MemoryTtlScheduler

        await MemoryTtlScheduler().start()
    except Exception as exc:
        logger.warning("[startup] Memory TTL scheduler failed to start: %s", exc)

async def _startup_memory_outbox_worker():
    """Resume durable post-response memory work accepted before a restart."""

    if not (settings.memory.enabled and settings.memory.layered_enabled):
        return
    try:
        from core.memory.outbox import MemoryOutboxWorker

        global _memory_outbox_worker
        _memory_outbox_worker = MemoryOutboxWorker()
        await _memory_outbox_worker.start()
        from core.infra import runtime_state

        runtime_state.register("memory_outbox_worker", _memory_outbox_worker)
        logger.info("[startup] memory outbox worker started")
    except Exception as exc:
        logger.warning("[startup] memory outbox worker failed to start: %s", exc)

async def _shutdown_memory_outbox_worker():
    global _memory_outbox_worker
    if _memory_outbox_worker is None:
        return
    try:
        await _memory_outbox_worker.stop()
    except Exception as exc:
        logger.warning("[shutdown] memory outbox worker stop failed: %s", exc)
    _memory_outbox_worker = None

async def _startup_persona_distill_worker():
    """Start the persona distillation queue worker.

    Per-worker, not singleton: it claims each job with FOR UPDATE SKIP LOCKED, which is
    exactly the criterion stated at the top of this file — and a user waiting on 发起蒸馏
    should not depend on which worker took the HTTP request.
    """
    try:
        from orchestration.workers.persona_distill_worker import PersonaDistillWorker

        global _persona_distill_worker
        _persona_distill_worker = PersonaDistillWorker()
        await _persona_distill_worker.start()
    except Exception as exc:
        logger.warning("[startup] persona distill worker failed to start: %s", exc)

async def _shutdown_persona_distill_worker():
    global _persona_distill_worker
    if _persona_distill_worker is not None:
        await _persona_distill_worker.stop()
        _persona_distill_worker = None

async def _startup_warmup_memory():
    """Prewarm the mem0 / Milvus connections in the background so the first request avoids cold-connection handshake latency.

    Fire-and-forget: does not block startup; failure does not affect service start.
    """
    if not settings.memory.enabled:
        return
    import asyncio as _asyncio

    async def _warmup() -> None:
        try:
            from core.memory.service import get_memory as _get_memory  # type: ignore[attr-defined]

            # _get_memory is thread-safe lazy loading; the first call establishes the Milvus + Neo4j connections
            instance = _get_memory()
            if instance is not None:
                logger.info("[startup] memory (mem0/milvus) connection warmed up")
            else:
                logger.info("[startup] memory enabled but initialization returned None")
        except Exception as exc:
            logger.warning("[startup] memory warmup failed: %s", exc)

    try:
        _asyncio.create_task(_warmup())
    except RuntimeError:
        # no running loop — should not happen in startup event
        logger.debug("[startup] no event loop for memory warmup")
