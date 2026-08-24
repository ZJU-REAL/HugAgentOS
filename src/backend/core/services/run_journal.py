"""Durable ownership, operation and recovery journal for ChatRun.

The database is the source of truth. Redis streams may be deleted or replayed
without changing whether work is accepted, owned, recoverable or terminal.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, sessionmaker

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ChatRunOperation, ChatSession

LIVE_STATUSES = ("pending", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
RECOVERABLE_PRE_MODEL_PHASES = ("accepted", "claimed", "pre_model")
RECOVERABLE_TOOL_PHASES = (
    "tool_intent_replay_safe",
    "tool_result_committed",
)
NON_CHAT_RUN_KINDS = ("plan_execute", "plan_generate", "autonomous_loop")
TOOL_BOUND_INTERNAL_RUN_KINDS = (
    "automation_plan",
    "automation_prompt",
    "batch_item",
    "internal_job_agent",
    "legacy_chat_stream",
)
SAFE_STOP_ORPHAN_KINDS = (*TOOL_BOUND_INTERNAL_RUN_KINDS, "plan_generate")


class RunJournalError(RuntimeError):
    pass


class RunNotFound(RunJournalError):
    pass


class RunLeaseLost(RunJournalError):
    pass


class RunAlreadyExists(RunJournalError):
    """A caller-scoped run identity was already accepted durably."""


@dataclass(frozen=True)
class DurableRunBinding:
    run_id: str
    owner: str
    chat_id: str


@asynccontextmanager
async def durable_run_binding(
    *,
    user_id: str,
    chat_id: Optional[str],
    kind: str,
    external_id: str,
    request_payload: Optional[Mapping[str, Any]] = None,
    recovery_snapshot: Optional[Mapping[str, Any]] = None,
    session_factory: sessionmaker = SessionLocal,
    lease_seconds: int = 300,
    heartbeat_interval: Optional[float] = None,
    binding_run_id: Optional[str] = None,
):
    """Give non-chat Agent entry points the same durable run/heartbeat contract."""

    actual_chat_id = str(chat_id or f"internal_{uuid.uuid4().hex[:24]}")
    with session_factory() as db:
        session = db.get(ChatSession, actual_chat_id)
        if session is None:
            db.add(
                ChatSession(
                    chat_id=actual_chat_id,
                    user_id=user_id,
                    title=f"{kind} run",
                )
            )
            db.commit()
        elif session.user_id != user_id:
            raise RunJournalError("durable run chat belongs to another user")

    run_id = str(binding_run_id or f"run_{uuid.uuid4().hex}")
    owner = f"{kind}:{uuid.uuid4().hex}"
    journal = RunJournal(session_factory)
    snapshot = {"kind": kind, "external_id": external_id}
    snapshot.update(dict(recovery_snapshot or {}))
    try:
        journal.accept(
            run_id=run_id,
            message_id=f"msg_{uuid.uuid4().hex}",
            chat_id=actual_chat_id,
            user_id=user_id,
            request_payload={
                "kind": kind,
                "external_id": external_id,
                **dict(request_payload or {}),
            },
            recovery_snapshot=snapshot,
        )
    except Exception as exc:
        # Deterministic identities are a tiny database-backed admission lock.
        # The primary key resolves the check-then-create race across processes;
        # only translate an error when that exact row now exists so unrelated
        # database failures retain their original signal.
        if binding_run_id:
            with session_factory() as db:
                if db.get(ChatRun, run_id) is not None:
                    raise RunAlreadyExists(run_id) from exc
        raise
    if not journal.claim(run_id, owner=owner, lease_seconds=lease_seconds):
        raise RunLeaseLost(run_id)

    lost = asyncio.Event()
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - async context always has a task
        raise RuntimeError("durable run binding has no asyncio task")

    async def _heartbeat() -> None:
        interval = heartbeat_interval
        if interval is None:
            interval = max(1.0, min(30.0, lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    journal.renew,
                    run_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                )
            except Exception:  # noqa: BLE001 - failed heartbeat means lost ownership
                renewed = False
            if not renewed:
                lost.set()
                worker_task.cancel()
                return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        yield DurableRunBinding(run_id=run_id, owner=owner, chat_id=actual_chat_id)
        if lost.is_set():
            raise RunLeaseLost(run_id)
    except asyncio.CancelledError:
        if not lost.is_set():
            journal.complete(
                run_id,
                owner=owner,
                status="cancelled",
                failure_reason=f"{kind} invocation cancelled",
            )
        raise
    except Exception as exc:
        from core.services.tool_effect_ledger import find_tool_outcome_unknown

        unknown = find_tool_outcome_unknown(exc)
        if unknown is not None:
            journal.needs_attention(
                run_id,
                owner=owner,
                reason=f"tool outcome pending policy recovery: {unknown}",
            )
        else:
            journal.complete(
                run_id,
                owner=owner,
                status="failed",
                failure_reason=f"{type(exc).__name__}: {exc}"[:2000],
            )
        raise
    else:
        journal.complete(run_id, owner=owner, status="completed")
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


@dataclass(frozen=True)
class JournalReceipt:
    run_id: str
    operation_seq: int
    snapshot_version: int
    phase: str


@dataclass(frozen=True)
class RecoveryDecision:
    run_id: str
    chat_id: str
    user_id: str
    message_id: str
    action: str
    phase: str
    snapshot: Mapping[str, Any]
    request_payload: Mapping[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RunJournal:
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

    def claim(self, run_id: str, *, owner: str, lease_seconds: int) -> bool:
        """Atomically acquire a free/expired lease; exactly one claimant wins."""
        if not owner:
            raise ValueError("owner is required")
        now = self._clock()
        expires = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._sessions() as db:
            affected = (
                db.query(ChatRun)
                .filter(
                    ChatRun.run_id == run_id,
                    ChatRun.status.in_(LIVE_STATUSES),
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

    def _locked_owned_run(self, db: Session, run_id: str, owner: str) -> ChatRun:
        row = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise RunNotFound(run_id)
        expires = _aware(row.lease_expires_at)
        now = _aware(self._clock())
        if (
            row.status not in LIVE_STATUSES
            or row.lease_owner != owner
            or expires is None
            or now is None
            or expires <= now
        ):
            raise RunLeaseLost(run_id)
        return row

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
        version = int(
            row.snapshot_version if snapshot_version is None else snapshot_version
        )
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
            row = self._locked_owned_run(db, run_id, owner)
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
    ) -> int:
        """Reserve the next durable SSE projection offset.

        Live projections require the current lease owner. Terminal projections
        are allowed only after a terminal CAS has already won. Redis may lose
        an entry, but offsets never reset or get reused across recovery workers.
        """
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
            row.last_event_offset = next_offset
            row.updated_at = now
            db.commit()
            return next_offset

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
            row.error_message = (
                failure_reason if status == "failed" else row.error_message
            )
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
                        committed_operation.get("operation_type")
                        or "side_effect_committed"
                    ),
                    phase=str(
                        committed_operation.get("phase") or "side_effect_committed"
                    ),
                    safety=str(
                        committed_operation.get("safety") or "side_effect_committed"
                    ),
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

    def cancel(self, run_id: str, *, reason: str) -> bool:
        now = self._clock()
        with self._sessions() as db:
            row = (
                db.query(ChatRun)
                .filter(ChatRun.run_id == run_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None or row.status not in LIVE_STATUSES:
                return False
            self._append_locked(
                db,
                row,
                owner="system:cancel",
                operation_type="run_cancelled",
                phase="cancelled",
                safety="terminal",
                payload={"reason": reason},
            )
            row.status = "cancelled"
            row.failure_reason = reason
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            db.commit()
            return True

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

    def recover(self) -> list[RecoveryDecision]:
        """Classify claimable orphans from the last committed DB safe point."""
        now = self._clock()
        decisions: list[RecoveryDecision] = []
        with self._sessions() as db:
            rows = (
                db.query(ChatRun)
                .filter(
                    ChatRun.status.in_(LIVE_STATUSES),
                    or_(
                        ChatRun.lease_owner.is_(None),
                        ChatRun.lease_expires_at.is_(None),
                        ChatRun.lease_expires_at <= now,
                    ),
                )
                .order_by(ChatRun.created_at, ChatRun.run_id)
                .with_for_update()
                .all()
            )
            for row in rows:
                snapshot = dict(row.recovery_snapshot or {})
                request_payload = dict(row.request_payload or {})
                phase = str(row.run_phase or "accepted")
                legacy_unrecoverable = (
                    phase == "legacy_unrecoverable"
                    and request_payload.get("kind") not in NON_CHAT_RUN_KINDS
                )
                snapshot_kind = snapshot.get("kind")
                if snapshot_kind in SAFE_STOP_ORPHAN_KINDS:
                    action = "needs_attention"
                    reason = f"{snapshot_kind} worker disappeared; owner-specific retry is required"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                    row.lease_owner = None
                    row.lease_expires_at = None
                    decisions.append(
                        RecoveryDecision(
                            run_id=row.run_id,
                            chat_id=row.chat_id,
                            user_id=row.user_id,
                            message_id=row.message_id,
                            action=action,
                            phase=phase,
                            snapshot=snapshot,
                            request_payload=request_payload,
                        )
                    )
                    continue
                if (
                    snapshot_kind != "chat"
                    and row.writer_slot == "main"
                    and phase == "accepted"
                ):
                    reason = "server restarted before the accepted run was prepared"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_failed",
                        phase="failed",
                        safety="terminal",
                        payload={"reason": reason},
                    )
                    row.status = "failed"
                    row.failure_reason = reason
                    row.error_message = reason
                    row.completed_at = now
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.writer_slot = None
                    row.updated_at = now
                    decisions.append(
                        RecoveryDecision(
                            run_id=row.run_id,
                            chat_id=row.chat_id,
                            user_id=row.user_id,
                            message_id=row.message_id,
                            action="failed",
                            phase=phase,
                            snapshot=snapshot,
                            request_payload=request_payload,
                        )
                    )
                    continue
                if snapshot_kind != "chat" and not legacy_unrecoverable:
                    continue
                action = "resume"
                if legacy_unrecoverable:
                    action = "needs_attention"
                    reason = "legacy live run has no durable recovery snapshot"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase == "message_committed":
                    action = "completed"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="recovered_after_message_commit",
                        phase="completed",
                        safety="terminal",
                        payload=None,
                    )
                    row.status = "completed"
                    row.completed_at = now
                elif (
                    phase == "tool_result_unknown"
                    or row.last_operation_safety == "unknown_side_effect"
                ):
                    action = "needs_attention"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={
                            "reason": "unknown tool result requires recovery decision"
                        },
                    )
                    row.status = "needs_attention"
                    row.failure_reason = (
                        "unknown tool result requires recovery decision"
                    )
                elif phase in RECOVERABLE_TOOL_PHASES:
                    # We can recover the exact adapter call, but AgentScope's
                    # provider-neutral continuation cursor is not durable yet.
                    # Re-running the original prompt could produce a new tool
                    # call id and duplicate the side effect, so stop safely.
                    action = "needs_attention"
                    reason = (
                        "tool effect was recovered, but exact model continuation "
                        "is unavailable; automatic prompt replay is unsafe"
                    )
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase == "model_completed" and snapshot:
                    action = "resume_from_snapshot"
                elif phase == "model_inflight":
                    action = "needs_attention"
                    reason = "model outcome is unknown; automatic replay is unsafe"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase not in RECOVERABLE_PRE_MODEL_PHASES:
                    action = "needs_attention"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": f"unsupported recovery phase: {phase}"},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = f"unsupported recovery phase: {phase}"

                if action in {"completed", "needs_attention"}:
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.writer_slot = None
                    row.updated_at = now
                decisions.append(
                    RecoveryDecision(
                        run_id=row.run_id,
                        chat_id=row.chat_id,
                        user_id=row.user_id,
                        message_id=row.message_id,
                        action=action,
                        phase=phase,
                        snapshot=snapshot,
                        request_payload=request_payload,
                    )
                )
            db.commit()
        return decisions


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
