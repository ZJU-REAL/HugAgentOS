"""Unified entry point for memory operation auditing.

All read/write operations across the L1 / L2 / L3 / session layers should go
through `record()` or `record_batch()` to write to the `memory_audit` table.
Failures do not bubble up; they only emit a warning (auditing must not block
the main flow).

Toggle: `MEMORY_AUDIT_ENABLED` env (default true). Forced on for government /
financial scenarios.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Iterable, Literal, Optional

from core.config.settings import settings
from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)


Action = Literal["read", "write", "update", "delete", "write_rejected", "forget"]
Layer = Literal["L1", "L2", "L3", "session", "batch"]


def _audit_enabled() -> bool:
    """Audit toggle (aligned with global settings, default true)."""
    from core.memory.edition import audit_enabled
    return audit_enabled()


def _content_hash(content: Optional[str]) -> Optional[str]:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


async def record(
    ctx: MemoryContext,
    action: Action,
    layer: Layer,
    *,
    memory_id: Optional[str] = None,
    content: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Write one audit record. Failures do not bubble up.

    content is used only to compute the hash, never stored; the original text
    is never written to the audit table.
    """
    if not settings.memory.enabled or not _audit_enabled():
        return

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_insert_one, ctx, action, layer, memory_id, content, reason)
    except Exception as exc:
        logger.warning("[memory_audit] record failed: %s", exc)


async def record_batch(
    ctx: MemoryContext,
    entries: Iterable[dict],
) -> None:
    """Batch-write audit records; each entry must contain `action` / `layer`, other fields optional."""
    if not settings.memory.enabled or not _audit_enabled():
        return

    rows = []
    for e in entries:
        rows.append({
            "actor": ctx.effective_actor,
            "action": e["action"],
            "layer": e["layer"],
            "memory_id": e.get("memory_id"),
            "workspace_id": ctx.workspace_id,
            "user_id": ctx.user_id,
            "chat_id": ctx.chat_id,
            "confidentiality": e.get("confidentiality") or ctx.confidentiality,
            "content_hash": _content_hash(e.get("content")),
            "reason": e.get("reason"),
        })
    if not rows:
        return

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_insert_many, rows)
    except Exception as exc:
        logger.warning("[memory_audit] record_batch failed (%d rows): %s", len(rows), exc)


def record_sync(
    ctx: MemoryContext,
    action: Action,
    layer: Layer,
    *,
    memory_id: Optional[str] = None,
    content: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Synchronously write one audit record (for non-async contexts, e.g. sync functions inside run_in_executor).

    The only difference from `record()` is the calling style: the former is
    async (internally goes through the executor), the latter inserts into the
    DB synchronously on the current thread. Both are gated by the
    `audit_enabled` toggle, and failures do not bubble up.
    """
    if not settings.memory.enabled or not _audit_enabled():
        return
    try:
        _sync_insert_one(ctx, action, layer, memory_id, content, reason)
    except Exception as exc:
        logger.debug("[memory_audit] record_sync failed: %s", exc)


def _sync_insert_one(
    ctx: MemoryContext,
    action: Action,
    layer: Layer,
    memory_id: Optional[str],
    content: Optional[str],
    reason: Optional[str],
) -> None:
    from core.db.engine import SessionLocal
    from core.db.models import MemoryAudit

    row = MemoryAudit(
        actor=ctx.effective_actor,
        action=action,
        layer=layer,
        memory_id=memory_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        chat_id=ctx.chat_id,
        confidentiality=ctx.confidentiality or "unknown",
        content_hash=_content_hash(content),
        reason=reason,
    )
    with SessionLocal() as session:
        session.add(row)
        session.commit()


def _sync_insert_many(rows: list[dict]) -> None:
    from core.db.engine import SessionLocal
    from core.db.models import MemoryAudit

    with SessionLocal() as session:
        session.add_all([MemoryAudit(**r) for r in rows])
        session.commit()
