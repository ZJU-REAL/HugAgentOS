"""UTC timestamps shared by run records and emitted events."""

from __future__ import annotations

import datetime as _datetime
from datetime import datetime
from typing import Optional

import core.db.engine as engine
import core.db.models as models
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return _datetime.datetime.now(_datetime.timezone.utc)


def _epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Wall-clock milliseconds for a stored timestamp.

    SQLite hands back naive datetimes; everything written here is UTC, so an
    absent tzinfo is read as UTC rather than as the host's local zone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_datetime.timezone.utc)
    return int(value.timestamp() * 1000)


def _run_started_at_ms(run_id: str) -> Optional[int]:
    """When this run actually began, in wall-clock milliseconds.

    Read from ``chat_runs.started_at``, which is written once when the run is
    claimed and is never rewritten by a recovering worker. Sending it with the
    first frame is what lets a second device — or a reloaded tab — join a run
    already in flight and show the true elapsed time instead of starting its
    own stopwatch from zero.

    Only a worker resuming someone else's run needs to look this up: a worker
    that claimed the run already holds the instant it wrote. Keeping the read
    off the normal send path matters — it sits directly in front of the first
    frame, where every millisecond is one the user spends staring at nothing.
    """
    with engine.SessionLocal() as db:
        row = (
            db.query(models.ChatRun.started_at, models.ChatRun.created_at)
            .filter(models.ChatRun.run_id == run_id)
            .first()
        )
    if row is None:
        return None
    return _epoch_ms(row.started_at or row.created_at)
