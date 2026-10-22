"""L2 expiry policy and bounded physical removal of expired memories.

mem0 stores expiration_date as ordinary metadata. Public L2 reads enforce
expiry immediately; the daily sweep only reclaims storage. Legacy ttl_days
rows fall back to their update/create timestamp. Invalid dates never trigger
physical deletion. Durable effect receipts remain readable after expiry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from core.config.settings import settings
from core.infra.time import as_utc, utc_now

logger = logging.getLogger(__name__)

# Bounded per run: a sweep is a hygiene pass, not a migration. Whatever a run
# doesn't reach, the next night's run will — expired entries are already hidden
# from retrieval either way, so physical deletion has no urgency.
DEFAULT_SCAN_LIMIT = 5000


def entry_expiry_date(payload: dict) -> Optional[date]:
    """The date this entry stops being valid, or None if it never expires.

    ``expiration_date`` (YYYY-MM-DD metadata) wins; legacy entries fall back to
    ``updated_at``/``created_at`` + ``ttl_days``. Unparseable data means "no
    expiry" — a sweeper must never delete on a guess.
    """
    if not isinstance(payload, dict):
        return None

    exp = payload.get("expiration_date")
    if exp:
        try:
            return date.fromisoformat(str(exp)[:10])
        except ValueError:
            pass

    ttl_days = payload.get("ttl_days")
    try:
        ttl = int(ttl_days)
    except (TypeError, ValueError):
        return None
    if ttl <= 0:
        return None

    stamp = payload.get("updated_at") or payload.get("created_at")
    if not stamp:
        return None
    try:
        base = as_utc(datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))).date()
    except ValueError:
        return None
    return base + timedelta(days=ttl)


def memory_entry_expired(item: dict) -> bool:
    """Evaluate a mem0 result or raw payload using the same UTC expiry policy."""
    if not isinstance(item, dict):
        return False
    payload = {**item, **(item.get("metadata") or {})}
    expiry = entry_expiry_date(payload)
    return expiry is not None and expiry <= utc_now().date()


async def sweep_expired_memories(scan_limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """Delete expired L2 entries. Returns ``{"scanned", "expired", "deleted"}``.

    Never raises: every failure degrades to "swept nothing tonight", which the
    next run retries. Expired-but-not-yet-deleted entries are already invisible
    to retrieval, so nothing depends on this succeeding promptly.
    """
    stats = {"scanned": 0, "expired": 0, "deleted": 0}
    if not settings.memory.enabled:
        return stats

    from core.memory.service import get_memory as _get_memory

    loop = asyncio.get_running_loop()
    memory = await loop.run_in_executor(None, _get_memory)
    if memory is None:
        return stats

    # Candidate rows straight from Milvus: the metadata JSON field carries the
    # whole mem0 payload. Scoped to L2 — L1 lives in the DB profile and TASK in
    # chat metadata; neither is swept here.
    try:
        rows = await loop.run_in_executor(
            None,
            lambda: memory.vector_store.client.query(
                collection_name=memory.vector_store.collection_name,
                filter='metadata["layer"] == "L2"',
                output_fields=["id", "metadata"],
                limit=scan_limit,
            ),
        )
    except Exception as exc:
        logger.warning("[memory-ttl] candidate query failed: %s", exc)
        return stats

    today = utc_now().date()
    expired_ids: list[str] = []
    for row in rows or []:
        stats["scanned"] += 1
        expiry = entry_expiry_date(row.get("metadata") or {})
        if expiry is not None and expiry <= today:
            row_id = row.get("id")
            if row_id:
                expired_ids.append(str(row_id))
    stats["expired"] = len(expired_ids)

    for memory_id in expired_ids:
        try:
            await loop.run_in_executor(None, lambda mid=memory_id: memory.delete(mid))
            stats["deleted"] += 1
        except Exception as exc:
            logger.warning("[memory-ttl] delete failed id=%s: %s", memory_id, exc)

    if stats["expired"]:
        logger.info("[memory-ttl] sweep: scanned=%d expired=%d deleted=%d",
                    stats["scanned"], stats["expired"], stats["deleted"])
    return stats
