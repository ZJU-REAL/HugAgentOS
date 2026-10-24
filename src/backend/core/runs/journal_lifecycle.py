"""Owned run terminal transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from core.db.models import ChatRun
from core.runs.journal_domain import LIVE_STATUSES, TERMINAL_STATUSES, RunLeaseLost, RunNotFound, _aware
from sqlalchemy.orm import Session


class RunLifecycle:
    def complete(
        self,
        run_id: str,
        *,
        owner: str,
        status: str,
        usage: Optional[Mapping[str, Any]] = None,
        failure_reason: Optional[str] = None,
        last_event_offset: Optional[int] = None,
        commit_effect: Optional[Callable[[Session], None]] = None,
        committed_operation: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        now = self._clock()
        with self._sessions() as db:
            try:
                row = self._locked_owned_run(db, run_id, owner)
            except (RunNotFound, RunLeaseLost):
                db.rollback()
                return False
            # Release the per-chat main-writer fence in the same transaction as
            # the terminal state. A queued successor may then acquire the slot
            # inside ``commit_effect`` without opening an admission gap.
            row.status = status
            row.usage = dict(usage or {}) or None
            row.failure_reason = failure_reason
            row.error_message = failure_reason if status == "failed" else row.error_message
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            row.writer_slot = None
            if last_event_offset is not None:
                row.last_event_offset = max(
                    int(row.last_event_offset or 0),
                    int(last_event_offset),
                )
            if commit_effect is not None:
                commit_effect(db)
            if committed_operation is not None:
                self._append_locked(
                    db,
                    row,
                    owner=owner,
                    operation_type=str(
                        committed_operation.get("operation_type") or "side_effect_committed"
                    ),
                    phase=str(committed_operation.get("phase") or "side_effect_committed"),
                    safety=str(committed_operation.get("safety") or "side_effect_committed"),
                    payload=(
                        dict(committed_operation["payload"])
                        if isinstance(committed_operation.get("payload"), Mapping)
                        else None
                    ),
                )
            self._append_locked(
                db,
                row,
                owner=owner,
                operation_type=f"run_{status}",
                phase=status,
                safety="terminal",
                payload={"failure_reason": failure_reason} if failure_reason else None,
            )
            db.commit()
            return True

    def cancel(
        self,
        run_id: str,
        *,
        reason: str,
        cancel_guard: Optional[Callable[[Session], bool]] = None,
    ) -> bool:
        now = self._clock()
        with self._sessions() as db:
            # The caller may lock its ownership row here. Keep that lock and
            # the child transition in one short transaction, without external I/O.
            if cancel_guard is not None and not cancel_guard(db):
                return False
            row = db.query(ChatRun).filter(ChatRun.run_id == run_id).with_for_update().one_or_none()
            if row is None or row.status not in LIVE_STATUSES:
                return False
            self.cancel_in_session(db, row, reason=reason, now=now)
            db.commit()
            return True

    @classmethod
    def cancel_in_session(cls, db: Session, row: ChatRun, *, reason: str, now: datetime) -> None:
        """Fence a locked live run without committing the caller's transaction."""
        cls._append_locked(
            db,
            row,
            owner="system:cancel",
            operation_type="run_cancelled",
            phase="cancelled",
            safety="terminal",
            payload={"reason": reason},
        )
        stopped_at = _aware(row.updated_at) if row.status == "needs_attention" else now
        row.status = "cancelled"
        row.failure_reason = reason
        row.completed_at = stopped_at or now
        row.updated_at = now
        row.lease_owner = None
        row.lease_expires_at = None

    def release(self, run_id: str, *, owner: str, reason: str) -> bool:
        """Relinquish a live run without declaring an ambiguous operation failed."""
        now = self._clock()
        with self._sessions() as db:
            try:
                row = self._locked_owned_run(db, run_id, owner)
            except (RunNotFound, RunLeaseLost):
                db.rollback()
                return False
            self._append_locked(
                db,
                row,
                owner=owner,
                operation_type="run_released_for_recovery",
                phase=str(row.run_phase or "accepted"),
                safety="recovery_required",
                payload={"reason": str(reason)},
            )
            row.status = "pending"
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            db.commit()
            return True

    def needs_attention(
        self,
        run_id: str,
        *,
        reason: str,
        owner: Optional[str] = None,
    ) -> bool:
        """Pause an owned or claimable run and journal why automation stopped."""
        now = self._clock()
        with self._sessions() as db:
            if owner is not None:
                try:
                    row = self._locked_owned_run(db, run_id, owner)
                except (RunNotFound, RunLeaseLost):
                    db.rollback()
                    return False
                operation_owner = owner
            else:
                row = (
                    db.query(ChatRun)
                    .filter(ChatRun.run_id == run_id)
                    .with_for_update()
                    .one_or_none()
                )
                if row is None or row.status not in LIVE_STATUSES:
                    return False
                expires = _aware(row.lease_expires_at)
                current = _aware(now)
                if (
                    row.lease_owner is not None
                    and expires is not None
                    and current is not None
                    and expires > current
                ):
                    return False
                operation_owner = "system:recovery"

            self._append_locked(
                db,
                row,
                owner=operation_owner,
                operation_type="run_needs_attention",
                phase="needs_attention",
                safety="manual_review",
                payload={"reason": str(reason)},
            )
            row.status = "needs_attention"
            row.failure_reason = str(reason)
            row.lease_owner = None
            row.lease_expires_at = None
            row.writer_slot = None
            row.updated_at = now
            db.commit()
            return True
