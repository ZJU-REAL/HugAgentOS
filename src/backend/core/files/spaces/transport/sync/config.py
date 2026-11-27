"""Limits for the synchronized space copy, read from the environment on use."""
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncLimits:
    max_total_bytes: int
    max_entries: int
    concurrency: int
    flush_interval_s: float
    # Per-file size comes from the actor's configured upload cap (see ``actor_file_limit_bytes``);
    # ``sys.maxsize`` = no per-file limit.
    max_file_bytes: int = sys.maxsize


def _positive(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def actor_file_limit_bytes(actor: str) -> int:
    """The actor's per-file upload cap in bytes, or ``sys.maxsize`` when none is configured."""
    from core.auth.capabilities import user_file_limit_bytes
    from core.db.engine import SessionLocal
    with SessionLocal() as db:
        return user_file_limit_bytes(db, actor) or sys.maxsize


def sync_limits() -> SyncLimits:
    return SyncLimits(
        max_total_bytes=_positive("HUGAGENT_SPACE_SYNC_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
        max_entries=_positive("HUGAGENT_SPACE_SYNC_MAX_ENTRIES", 20000),
        concurrency=_positive("HUGAGENT_SPACE_SYNC_CONCURRENCY", 8),
        flush_interval_s=float(_positive("HUGAGENT_SPACE_SYNC_FLUSH_INTERVAL_S", 60)),
    )
