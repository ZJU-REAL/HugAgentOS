"""Snapshot persistence: restore points, in-flight snapshot jobs and lifecycle states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


def _get_db_session_local():
    from core.db.engine import SessionLocal

    return SessionLocal


def _get_snapshot_model():
    from core.db.models import ChatSandboxSnapshot

    return ChatSandboxSnapshot


# OpenSandbox snapshot lifecycle states (the status.state returned by GET /v1/snapshots/{id}).
# SDK 0.1.13 does not expose an enum, so we can only string-compare these values.
_SNAPSHOT_STATE_READY = "Ready"

_SNAPSHOT_TERMINAL_FAILURE_STATES = frozenset({"Failed", "Error"})

# Not a controller state: GET /v1/snapshots/{id} answered 404.
_SNAPSHOT_STATE_MISSING = "Missing"


# ───── snapshot jobs and pending deletions ────────────────────────────────
# Synchronous SQLAlchemy; callers run them through ``asyncio.to_thread``.
#
# Two books, kept apart on purpose. ``chat_sandbox_snapshot_jobs`` says where a
# chat's snapshot attempt stands and gates the next one. ``sandbox_snapshot_
# deletions`` lists controller snapshots nothing references any more. A remote
# delete that keeps failing therefore never blocks a chat from being parked.

JOB_CREATING = "creating"
JOB_FAILED = "failed"


@dataclass(frozen=True)
class SnapshotJob:
    chat_id: str
    sandbox_id: str
    snapshot_id: Optional[str]
    state: str
    started_at: datetime
    failures: int
    retry_after: Optional[datetime]

    def may_retry(self, now: datetime) -> bool:
        return self.state == JOB_FAILED and (self.retry_after is None or self.retry_after <= now)


def _get_job_model():
    from core.db.models import ChatSandboxSnapshotJob

    return ChatSandboxSnapshotJob


def _get_deletion_model():
    from core.db.models import SandboxSnapshotDeletion

    return SandboxSnapshotDeletion


def _as_job(row) -> SnapshotJob:
    return SnapshotJob(
        chat_id=row.chat_id,
        sandbox_id=row.sandbox_id,
        snapshot_id=row.snapshot_id,
        state=row.state,
        started_at=row.started_at,
        failures=row.failures or 0,
        retry_after=row.retry_after,
    )


def _queue_deletion(db, snapshot_id: str, chat_id: str, reason: str) -> None:
    Deletion = _get_deletion_model()
    if db.get(Deletion, snapshot_id) is None:
        db.add(Deletion(snapshot_id=snapshot_id, chat_id=chat_id, reason=reason[:200]))


def get_job(chat_id: str) -> Optional[SnapshotJob]:
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        row = db.get(Job, chat_id)
        return _as_job(row) if row is not None else None


def load_jobs() -> dict[str, SnapshotJob]:
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        return {row.chat_id: _as_job(row) for row in db.query(Job).all()}


def record_creating(
    chat_id: str, *, sandbox_id: str, snapshot_id: str, started_at: datetime, failures: int,
) -> None:
    """The controller accepted a snapshot: from now on its outcome is tracked here."""
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        row = db.get(Job, chat_id)
        if row is None:
            row = Job(chat_id=chat_id)
            db.add(row)
        elif row.state == JOB_CREATING:
            # Never overwrite a tracked attempt: the newer one becomes garbage.
            _queue_deletion(db, snapshot_id, chat_id, "superseded start")
            db.commit()
            return
        row.sandbox_id = sandbox_id
        row.snapshot_id = snapshot_id
        row.state = JOB_CREATING
        row.started_at = started_at
        row.failures = failures
        row.retry_after = None
        row.last_error = None
        db.commit()


def record_failure(
    chat_id: str,
    *,
    sandbox_id: str,
    attempt_snapshot_id: Optional[str],
    remote_left: bool,
    started_at: datetime,
    error: str,
    retry_after: Callable[[int], datetime],
) -> None:
    """Back the chat off after the attempt ``attempt_snapshot_id`` failed.

    ``attempt_snapshot_id`` is None when the controller refused to start one.
    With ``remote_left`` the snapshot is queued for deletion in the same
    transaction. ``retry_after`` maps the new failure count to the next attempt.
    """
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        row = db.get(Job, chat_id)
        if row is None:
            row = Job(chat_id=chat_id, failures=0)
            db.add(row)
        elif row.snapshot_id != attempt_snapshot_id:
            return  # superseded by a newer attempt
        if remote_left and attempt_snapshot_id:
            _queue_deletion(db, attempt_snapshot_id, chat_id, error)
        row.sandbox_id = sandbox_id
        row.snapshot_id = None
        row.state = JOB_FAILED
        row.started_at = started_at
        row.failures = (row.failures or 0) + 1
        row.retry_after = retry_after(row.failures)
        row.last_error = error[:500]
        db.commit()


def discard_job(chat_id: str, snapshot_id: str, reason: str) -> None:
    """The snapshot no longer matches the chat: drop the job, queue the snapshot."""
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        row = db.get(Job, chat_id)
        if row is None or row.snapshot_id != snapshot_id:
            return
        _queue_deletion(db, snapshot_id, chat_id, reason)
        db.delete(row)
        db.commit()


def forget_job(chat_id: str) -> None:
    Job = _get_job_model()
    with _get_db_session_local()() as db:
        db.query(Job).filter(Job.chat_id == chat_id).delete()
        db.commit()


def promote_job(chat_id: str, snapshot_id: str, *, expires_at: datetime) -> Optional[str]:
    """Make a Ready snapshot the chat's restore point, in one transaction.

    The replaced restore point is queued for deletion in the same transaction,
    so it is deleted even if the process dies first. Returns its id.
    """
    Job = _get_job_model()
    Pointer = _get_snapshot_model()
    with _get_db_session_local()() as db:
        job = db.get(Job, chat_id)
        if job is None or job.snapshot_id != snapshot_id:
            raise LookupError(f"snapshot job {chat_id}/{snapshot_id} is gone")
        pointer = db.get(Pointer, chat_id)
        old_snapshot_id = pointer.snapshot_id if pointer is not None else None
        if pointer is None:
            pointer = Pointer(chat_id=chat_id)
            db.add(pointer)
        pointer.snapshot_id = snapshot_id
        pointer.sandbox_id = job.sandbox_id
        pointer.created_at = datetime.now(timezone.utc)
        pointer.expires_at = expires_at
        if old_snapshot_id == snapshot_id:
            old_snapshot_id = None
        if old_snapshot_id:
            _queue_deletion(db, old_snapshot_id, chat_id, "replaced restore point")
        db.delete(job)
        db.commit()
        return old_snapshot_id


def load_deletions(limit: int) -> list[str]:
    Deletion = _get_deletion_model()
    with _get_db_session_local()() as db:
        rows = db.query(Deletion).order_by(Deletion.attempts, Deletion.queued_at).limit(limit)
        return [row.snapshot_id for row in rows]


def deletion_done(snapshot_id: str) -> None:
    Deletion = _get_deletion_model()
    with _get_db_session_local()() as db:
        db.query(Deletion).filter(Deletion.snapshot_id == snapshot_id).delete()
        db.commit()


def deletion_failed(snapshot_id: str, error: str) -> int:
    Deletion = _get_deletion_model()
    with _get_db_session_local()() as db:
        row = db.get(Deletion, snapshot_id)
        if row is None:
            return 0
        row.attempts = (row.attempts or 0) + 1
        row.last_error = error[:500]
        db.commit()
        return row.attempts
