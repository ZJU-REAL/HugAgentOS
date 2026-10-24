"""Periodic remote MCP marketplace drift monitor."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from core.config.settings import settings
from core.db.threads import run_in_session
from features.marketplace.models import McpMarketItem
from features.marketplace.mcp_revalidation import revalidate_market_item

logger = logging.getLogger(__name__)
_task: asyncio.Task | None = None


def _interval_seconds() -> float:
    return float(settings.server.mcp_market_revalidate_interval)


def _monitored_slugs(db) -> list[str]:
    return [
        str(row[0])
        for row in db.query(McpMarketItem.slug)
        .filter(
            McpMarketItem.deleted_at.is_(None),
            McpMarketItem.status.in_(["active", "changed"]),
        )
        .all()
    ]


async def _run() -> None:
    interval = _interval_seconds()
    await asyncio.sleep(min(60.0, interval))
    while True:
        try:
            for slug in await run_in_session(_monitored_slugs):
                try:
                    await revalidate_market_item(slug)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "mcp_market_revalidate_failed", extra={"slug": slug, "error": str(exc)}
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp_market_monitor_cycle_failed", extra={"error": str(exc)})
        await asyncio.sleep(interval)


def start_monitor() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run(), name="mcp-marketplace-monitor")


async def stop_monitor() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with suppress(asyncio.CancelledError):
        await _task
    _task = None
