"""Durable append-only harness events and physical usage attempts."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.db.engine import SessionLocal
from core.db.models import (
    HarnessEventCursor,
    HarnessEventLog,
    HarnessUsageAttempt,
    HarnessUsageCursor,
)
from core.harness.events import Event, freeze_value, thaw_value
from core.harness.usage import AttemptUsage, UsageAttempt

logger = logging.getLogger(__name__)


def _insert_cursor_if_missing(db, model: type[Any], values: dict[str, Any]) -> None:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
    elif dialect == "postgresql":
        statement = pg_insert(model).values(**values).on_conflict_do_nothing()
    else:
        if db.get(model, values["run_id"]) is not None:
            return
        db.add(model(**values))
        db.flush()
        return
    db.execute(statement)


def _reserve(db, model: type[Any], run_id: str, column) -> int:
    _insert_cursor_if_missing(db, model, {"run_id": run_id, column.key: 0})
    value = db.execute(
        update(model)
        .where(model.run_id == run_id)
        .values({column.key: column + 1})
        .returning(column)
        .execution_options(synchronize_session=False)
    ).scalar_one()
    return int(value)


class HarnessUsageLedger:
    """Append attempts and derive aggregates without mutable summary rows."""

    def __init__(self, session_factory=SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _validate(attempt: UsageAttempt) -> None:
        if not attempt.run_id:
            raise ValueError("run_id is required")
        if attempt.kind not in {"model", "tool", "hook"}:
            raise ValueError(f"invalid attempt kind: {attempt.kind}")
        if attempt.status not in {"success", "failed", "timeout", "cancelled"}:
            raise ValueError(f"invalid attempt status: {attempt.status}")

    def record_attempt(self, attempt: UsageAttempt) -> UsageAttempt:
        self._validate(attempt)
        with self._session_factory() as db:
            seq = _reserve(
                db,
                HarnessUsageCursor,
                attempt.run_id,
                HarnessUsageCursor.next_attempt_seq,
            )
            retry_of = attempt.retry_of
            if retry_of is None and attempt.kind == "tool" and attempt.effect_id:
                query = db.query(HarnessUsageAttempt).filter(
                    HarnessUsageAttempt.run_id == attempt.run_id,
                    HarnessUsageAttempt.kind == attempt.kind,
                    HarnessUsageAttempt.operation_name == attempt.operation_name,
                    HarnessUsageAttempt.status.in_(("failed", "timeout")),
                )
                query = query.filter(HarnessUsageAttempt.effect_id == attempt.effect_id)
                previous = query.order_by(
                    HarnessUsageAttempt.attempt_seq.desc()
                ).first()
                retry_of = int(previous.attempt_seq) if previous is not None else None
            row = HarnessUsageAttempt(
                attempt_id=f"hat_{uuid.uuid4().hex}",
                run_id=attempt.run_id,
                attempt_seq=seq,
                kind=attempt.kind,
                operation_name=attempt.operation_name,
                provider=attempt.provider,
                model=attempt.model,
                effect_id=attempt.effect_id or None,
                prompt_tokens=max(0, int(attempt.usage.prompt_tokens)),
                completion_tokens=max(0, int(attempt.usage.completion_tokens)),
                cache_read_tokens=max(0, int(attempt.usage.cache_read_tokens)),
                cache_write_tokens=max(0, int(attempt.usage.cache_write_tokens)),
                latency_ms=max(0, int(attempt.latency_ms)),
                status=attempt.status,
                retry_of=retry_of,
                attempt_metadata=thaw_value(attempt.metadata),
            )
            db.add(row)
            db.commit()
            return replace(attempt, attempt_seq=seq, retry_of=retry_of)

    def attempts(self, run_id: str) -> tuple[UsageAttempt, ...]:
        with self._session_factory() as db:
            rows = (
                db.query(HarnessUsageAttempt)
                .filter(HarnessUsageAttempt.run_id == run_id)
                .order_by(HarnessUsageAttempt.attempt_seq)
                .all()
            )
            return tuple(
                UsageAttempt(
                    run_id=row.run_id,
                    kind=row.kind,
                    operation_name=row.operation_name,
                    status=row.status,
                    latency_ms=int(row.latency_ms or 0),
                    provider=row.provider or "",
                    model=row.model or "",
                    effect_id=row.effect_id or "",
                    retry_of=row.retry_of,
                    usage=AttemptUsage(
                        prompt_tokens=int(row.prompt_tokens or 0),
                        completion_tokens=int(row.completion_tokens or 0),
                        cache_read_tokens=int(row.cache_read_tokens or 0),
                        cache_write_tokens=int(row.cache_write_tokens or 0),
                    ),
                    metadata=freeze_value(row.attempt_metadata or {}),
                    attempt_seq=int(row.attempt_seq),
                )
                for row in rows
            )

    def aggregate(self, run_id: str) -> dict[str, Any]:
        rows = self.attempts(run_id)
        by_kind = {
            kind: sum(row.kind == kind for row in rows)
            for kind in ("model", "tool", "hook")
        }
        return {
            "attempt_count": len(rows),
            "failed_attempts": sum(row.status != "success" for row in rows),
            "by_kind": by_kind,
            "prompt_tokens": sum(row.usage.prompt_tokens for row in rows),
            "completion_tokens": sum(row.usage.completion_tokens for row in rows),
            "cache_read_tokens": sum(row.usage.cache_read_tokens for row in rows),
            "cache_write_tokens": sum(row.usage.cache_write_tokens for row in rows),
            "total_tokens": sum(row.usage.total_tokens for row in rows),
            "latency_ms": sum(row.latency_ms for row in rows),
        }


class DurableEventStore:
    """Callable storage adapter for :class:`core.harness.events.EventSink`."""

    def __init__(self, session_factory=SessionLocal) -> None:
        self._session_factory = session_factory

    def __call__(self, event: Event) -> Event:
        if not event.run_id:
            return event
        with self._session_factory() as db:
            seq = _reserve(
                db,
                HarnessEventCursor,
                event.run_id,
                HarnessEventCursor.next_event_seq,
            )
            db.add(
                HarnessEventLog(
                    event_id=f"hev_{uuid.uuid4().hex}",
                    run_id=event.run_id,
                    event_seq=seq,
                    event_type=event.event_type,
                    phase=event.phase,
                    payload=thaw_value(event.payload),
                    created_at=event.created_at,
                )
            )
            db.commit()
            return Event(
                run_id=event.run_id,
                event_type=event.event_type,
                phase=event.phase,
                payload=freeze_value(event.payload),
                created_at=event.created_at,
                event_seq=seq,
            )
