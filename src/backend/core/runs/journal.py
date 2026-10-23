"""Durable ownership, operation and recovery journal for ChatRun.

The database is the source of truth. Redis streams may be deleted or replayed
without changing whether work is accepted, owned, recoverable or terminal.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ChatRunOperation
from core.runs.binding import durable_run_binding
from core.runs.journal_domain import LIVE_STATUSES, DurableRunBinding, JournalReceipt, RecoveryDecision, RunAlreadyExists, RunJournalError, RunLeaseLost, RunNotFound, _aware, _utcnow
from core.runs.journal_lifecycle import RunLifecycle
from core.runs.journal_recovery import RunRecovery
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, sessionmaker, undefer


class RunJournal(RunLifecycle, RunRecovery):
    """Small transaction boundary for the Harness 4.1 run journal."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    def accept(
        self,
        *,
        run_id: str,
        message_id: str,
        chat_id: str,
        user_id: str,
        request_payload: Mapping[str, Any],
        recovery_snapshot: Optional[Mapping[str, Any]] = None,
        on_accept=None,
    ) -> ChatRun:
        """Commit acceptance before any in-process worker is created."""
        with self._sessions() as db:
            row = self.accept_in_session(
                db,
                run_id=run_id,
                message_id=message_id,
                chat_id=chat_id,
                user_id=user_id,
                request_payload=request_payload,
                recovery_snapshot=recovery_snapshot,
                now=self._clock(),
            )
            if on_accept is not None:
                on_accept(db, row)
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row

    @staticmethod
    def accept_in_session(
        db: Session,
        *,
        run_id: str,
        message_id: str,
        chat_id: str,
        user_id: str,
        request_payload: Mapping[str, Any],
        recovery_snapshot: Optional[Mapping[str, Any]] = None,
        user_message_id: Optional[str] = None,
        user_chat_seq: Optional[int] = None,
        assistant_chat_seq: Optional[int] = None,
        writer_slot: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ChatRun:
        """Stage a pending run inside the caller's existing transaction."""
        accepted_at = now or _utcnow()
        from core.auth.api_key_runs import bind_request_key, require_admission_key

        request_payload = bind_request_key(request_payload, user_id)
        require_admission_key(db, request_payload, user_id, accepted_at)
        snapshot = dict(recovery_snapshot or {})
        row = ChatRun(
            run_id=run_id,
            message_id=message_id,
            chat_id=chat_id,
            user_id=user_id,
            user_message_id=user_message_id,
            user_chat_seq=user_chat_seq,
            assistant_chat_seq=assistant_chat_seq,
            writer_slot=writer_slot,
            status="pending",
            request_payload=dict(request_payload or {}),
            last_event_offset=0,
            run_phase="accepted",
            operation_seq=0,
            snapshot_version=1 if snapshot else 0,
            recovery_snapshot=snapshot or None,
            last_operation_safety="replayable",
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        db.add(row)
        db.flush()
        return row

    def claim(self, run_id: str, *, owner: str, lease_seconds: int, pending_only: bool = False) -> bool:
        """Atomically acquire a free/expired lease; exactly one claimant wins."""
        if not owner:
            raise ValueError("owner is required")
        now = self._clock()
        expires = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._sessions() as db:
            from core.auth.api_key_runs import authorize_run_execution

            if not authorize_run_execution(db, run_id, now):
                return False
            affected = (
                db.query(ChatRun)
                .filter(
                    ChatRun.run_id == run_id,
                    ChatRun.status.in_(("pending",) if pending_only else LIVE_STATUSES),
                    or_(
                        ChatRun.lease_owner.is_(None),
                        ChatRun.lease_expires_at.is_(None),
                        ChatRun.lease_expires_at <= now,
                        ChatRun.lease_owner == owner,
                    ),
                )
                .update(
                    {
                        ChatRun.status: "running",
                        ChatRun.lease_owner: owner,
                        ChatRun.lease_expires_at: expires,
                        ChatRun.started_at: func.coalesce(ChatRun.started_at, now),
                        ChatRun.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return bool(affected)

    def renew(self, run_id: str, *, owner: str, lease_seconds: int) -> bool:
        now = self._clock()
        expires = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._sessions() as db:
            from core.auth.api_key_runs import authorize_run_execution

            if not authorize_run_execution(db, run_id, now):
                return False
            affected = (
                db.query(ChatRun)
                .filter(
                    ChatRun.run_id == run_id,
                    ChatRun.status.in_(LIVE_STATUSES),
                    ChatRun.lease_owner == owner,
                    ChatRun.lease_expires_at > now,
                )
                .update(
                    {
                        ChatRun.lease_expires_at: expires,
                        ChatRun.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return bool(affected)

    def _holds_lease(self, row: Any, owner: str) -> bool:
        expires = _aware(row.lease_expires_at)
        now = _aware(self._clock())
        return not (
            row.status not in LIVE_STATUSES
            or row.lease_owner != owner
            or expires is None
            or now is None
            or expires <= now
        )

    def _locked_owned_run(
        self, db: Session, run_id: str, owner: str, *, with_snapshot: bool = False
    ) -> ChatRun:
        query = db.query(ChatRun).filter(ChatRun.run_id == run_id)
        if with_snapshot:
            query = query.options(undefer(ChatRun.recovery_snapshot))
        row = query.with_for_update().one_or_none()
        if row is None:
            raise RunNotFound(run_id)
        if not self._holds_lease(row, owner):
            raise RunLeaseLost(run_id)
        return row

    def require_lease(self, run_id: str, owner: str) -> None:
        """Raise unless ``owner`` still holds a live lease on ``run_id``.

        The same check ``_locked_owned_run`` makes, without the write it exists
        to protect. An emitter fences every event, so this has to be a read: a
        write transaction per event is a disk flush per token on a device whose
        run journal is SQLite, and the person is watching the answer appear.
        """
        with self._sessions() as db:
            row = (
                db.query(ChatRun.status, ChatRun.lease_owner, ChatRun.lease_expires_at)
                .filter(ChatRun.run_id == run_id)
                .one_or_none()
            )
            if row is None:
                raise RunNotFound(run_id)
            if not self._holds_lease(row, owner):
                raise RunLeaseLost(run_id)

    @staticmethod
    def _append_locked(
        db: Session,
        row: ChatRun,
        *,
        owner: str,
        operation_type: str,
        phase: str,
        safety: str,
        payload: Optional[Mapping[str, Any]],
        snapshot_version: Optional[int] = None,
    ) -> JournalReceipt:
        next_seq = int(row.operation_seq or 0) + 1
        version = int(row.snapshot_version if snapshot_version is None else snapshot_version)
        db.add(
            ChatRunOperation(
                run_id=row.run_id,
                operation_seq=next_seq,
                operation_type=operation_type,
                phase=phase,
                safety=safety,
                owner=owner,
                snapshot_version=version,
                payload=dict(payload or {}) or None,
            )
        )
        row.operation_seq = next_seq
        row.run_phase = phase
        row.last_operation_safety = safety
        return JournalReceipt(
            run_id=row.run_id,
            operation_seq=next_seq,
            snapshot_version=version,
            phase=phase,
        )

    def append_operation(
        self,
        run_id: str,
        *,
        owner: str,
        operation_type: str,
        phase: str,
        safety: str,
        payload: Optional[Mapping[str, Any]] = None,
        commit_effect: Optional[Callable[[Session], None]] = None,
    ) -> JournalReceipt:
        now = self._clock()
        with self._sessions() as db:
            row = self._locked_owned_run(db, run_id, owner)
            if commit_effect is not None:
                commit_effect(db)
            receipt = self._append_locked(
                db,
                row,
                owner=owner,
                operation_type=operation_type,
                phase=phase,
                safety=safety,
                payload=payload,
            )
            row.updated_at = now
            db.commit()
            return receipt

    def save_snapshot(
        self,
        run_id: str,
        *,
        owner: str,
        phase: str,
        snapshot: Mapping[str, Any],
        safety: str,
    ) -> JournalReceipt:
        now = self._clock()
        with self._sessions() as db:
            row = self._locked_owned_run(db, run_id, owner, with_snapshot=True)
            version = int(row.snapshot_version or 0) + 1
            merged = dict(row.recovery_snapshot or {})
            merged.update(dict(snapshot or {}))
            row.recovery_snapshot = merged
            row.snapshot_version = version
            receipt = self._append_locked(
                db,
                row,
                owner=owner,
                operation_type="snapshot_saved",
                phase=phase,
                safety=safety,
                payload={"snapshot_keys": sorted(snapshot)},
                snapshot_version=version,
            )
            row.updated_at = now
            db.commit()
            return receipt

    def allocate_event_offset(
        self,
        run_id: str,
        *,
        owner: Optional[str] = None,
        terminal: bool = False,
        count: int = 1,
    ) -> int:
        """Reserve ``count`` consecutive durable SSE projection offsets; return the first.

        Live projections require the current lease owner. Terminal projections
        are allowed only after a terminal CAS has already won. Redis may lose
        an entry, but offsets never reset or get reused across recovery workers.

        A reservation is one serialized write, so an emitter that knows it is
        about to produce many events asks for a run of offsets and pays for one
        write instead of one per event. Offsets a reservation leaves unused are
        never emitted: readers order by offset and do not require them to be
        dense, and the next reservation still starts above them.
        """
        if count < 1:
            raise ValueError("count must be positive")
        now = self._clock()
        with self._sessions() as db:
            if terminal:
                row = (
                    db.query(ChatRun)
                    .filter(ChatRun.run_id == run_id)
                    .with_for_update()
                    .one_or_none()
                )
                if row is None:
                    raise RunNotFound(run_id)
                if row.status in LIVE_STATUSES:
                    raise RunJournalError(f"run {run_id} is not terminal")
            else:
                if not owner:
                    raise ValueError("owner is required for a live projection")
                row = self._locked_owned_run(db, run_id, owner)
            next_offset = int(row.last_event_offset or 0) + 1
            row.last_event_offset = next_offset + count - 1
            row.updated_at = now
            db.commit()
            return next_offset


__all__ = [
    "DurableRunBinding",
    "JournalReceipt",
    "RecoveryDecision",
    "RunAlreadyExists",
    "RunJournal",
    "RunJournalError",
    "RunLeaseLost",
    "RunNotFound",
    "durable_run_binding",
]
