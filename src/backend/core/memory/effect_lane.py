"""Cross-process serialization for ordered L2 external effects."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from core.db.engine import SessionLocal
from core.db.models import MemoryOutbox
from core.memory.executor import run_effect
from sqlalchemy import text

_local_locks: dict[str, threading.Lock] = {}
_local_locks_guard = threading.Lock()


class EffectLaneDeferred(RuntimeError):
    def __init__(self, retry_at: datetime) -> None:
        super().__init__("an older L2 effect must finish first")
        self.retry_at = retry_at


def _local_lock(scope_key: str) -> threading.Lock:
    with _local_locks_guard:
        return _local_locks.setdefault(scope_key, threading.Lock())


def _advisory_key(scope_key: str) -> int:
    return int.from_bytes(
        hashlib.sha256(scope_key.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _oldest_nonterminal(db, scope_key: str):
    oldest = (
        db.query(
            MemoryOutbox.id,
            MemoryOutbox.next_attempt_at,
            MemoryOutbox.lease_expires_at,
        )
        .filter(
            MemoryOutbox.scope_key == scope_key,
            MemoryOutbox.layer == "L2:procedural",
            MemoryOutbox.status.notin_(("succeeded", "quarantined")),
        )
        .order_by(MemoryOutbox.created_at.asc(), MemoryOutbox.id.asc())
        .first()
    )
    return oldest


@asynccontextmanager
async def ordered_l2_effect(scope_key: str, job_id: str):
    """Wait for this candidate's turn, then hold one lane through its effect.

    PostgreSQL uses a transaction-scoped advisory lock, which the server
    releases if the process dies. SQLite tests use a process-local lock. After
    acquiring, the candidate must still be the oldest non-terminal row in its
    scope; a later worker can therefore never overtake a crashed job and erase
    its external receipt before recovery checks it.
    """

    db = SessionLocal()
    dialect = db.bind.dialect.name if db.bind is not None else ""
    local = _local_lock(scope_key) if dialect != "postgresql" else None
    acquired = False
    try:
        if local is not None:
            while not local.acquire(blocking=False):
                await asyncio.sleep(0.05)
        else:
            await run_effect(
                db.execute,
                text("SELECT pg_advisory_xact_lock(:lane_key)"),
                {"lane_key": _advisory_key(scope_key)},
            )
        acquired = True
        oldest = _oldest_nonterminal(db, scope_key)
        if oldest is None or str(oldest[0]) != job_id:
            retry_at = (oldest[1] or oldest[2]) if oldest is not None else None
            if retry_at is None:
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            raise EffectLaneDeferred(retry_at)
        try:
            yield
        except BaseException:
            await run_effect(db.rollback)
            raise
        else:
            await run_effect(db.commit)
    except asyncio.CancelledError:
        if acquired:
            await run_effect(db.rollback)
        raise
    finally:
        await run_effect(db.close)
        if local is not None and acquired:
            local.release()
