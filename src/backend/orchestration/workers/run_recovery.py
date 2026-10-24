"""Periodic recovery and stale-run maintenance."""

from __future__ import annotations

import asyncio

import core.chat.runtime.recovery as recovery
import core.db.engine as engine
import core.runs.settings as settings
import core.runs.watchdog as watchdog
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def run_stale_reaper_loop() -> None:
    """Recover expired leases frequently and run the slower stale watchdog."""
    loop = asyncio.get_running_loop()
    next_reap_at = loop.time() + settings.STALE_REAPER_INTERVAL_SEC
    while True:
        try:
            await asyncio.sleep(settings.RUN_RECOVERY_INTERVAL_SEC)
            await recovery.recover_orphan_runs()
            if loop.time() >= next_reap_at:
                await watchdog.reap_stale_runs()
                from core.runs.tool_effects.journal import ToolEffectJournal

                pruned = await asyncio.to_thread(
                    ToolEffectJournal(engine.SessionLocal).prune_settled
                )
                if pruned:
                    logger.info("tool_effect_ledger_pruned", count=pruned)
                next_reap_at = loop.time() + settings.STALE_REAPER_INTERVAL_SEC
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("chat_run_stale_reaper_iteration_failed", exc_info=True)
