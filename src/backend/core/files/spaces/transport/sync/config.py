"""Limits for the synchronized space copy, read from the environment on use."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncLimits:
    max_file_bytes: int
    max_total_bytes: int
    max_entries: int
    concurrency: int
    flush_interval_s: float


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


def sync_limits() -> SyncLimits:
    return SyncLimits(
        max_file_bytes=_positive("HUGAGENT_SPACE_SYNC_MAX_FILE_BYTES", 100 * 1024 * 1024),
        max_total_bytes=_positive("HUGAGENT_SPACE_SYNC_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
        max_entries=_positive("HUGAGENT_SPACE_SYNC_MAX_ENTRIES", 20000),
        concurrency=_positive("HUGAGENT_SPACE_SYNC_CONCURRENCY", 8),
        flush_interval_s=float(_positive("HUGAGENT_SPACE_SYNC_FLUSH_INTERVAL_S", 60)),
    )
