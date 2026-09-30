"""Short, locked job transactions. Never retain a DB connection across sandbox I/O."""

import uuid
from datetime import datetime, timedelta, timezone

from core.db.engine import SessionLocal
from core.db.models import Job
from core.services.job_service import JobService, mint_token
from sqlalchemy.orm.attributes import flag_modified

OWNER = uuid.uuid4().hex
LEASE_SECONDS = 90
LIVE = ("pending", "running")
TERMINAL = ("completed", "failed", "cancelled", "interrupted", "paused")


def now():
    return datetime.now(timezone.utc)


def locked(db, jid):
    return db.query(Job).filter(Job.job_id == jid).with_for_update().populate_existing().first()


def snapshot(jid):
    with SessionLocal() as db:
        svc = JobService(db)
        job = svc.get(jid)
        if job is None:
            raise ValueError("job 不存在")
        return dict(
            job_id=jid,
            user_id=job.user_id,
            chat_id=job.chat_id,
            session_id=job.sandbox_session_id,
            status=job.status,
            name=job.name,
            script=job.script_text,
            budget=dict(job.budget or {}),
            usage=dict(job.usage or {}),
            meta=dict(job.extra_data or {}),
            created_at=job.created_at,
            error=job.error_message,
            stats=svc.stats(jid),
            budget_left=svc.budget_left(jid),
        )


def save_execution(jid, attempt, *, require_owner=True, **changes):
    with SessionLocal() as db:
        row = locked(db, jid)
        if row is None:
            return False
        meta = dict(row.extra_data or {})
        execution = dict(meta.get("execution") or {})
        if execution.get("attempt_id") != attempt or (
            require_owner and execution.get("owner") != OWNER
        ):
            return False
        execution.update(changes)
        meta["execution"] = execution
        row.extra_data = meta
        flag_modified(row, "extra_data")
        db.commit()
        return True


def begin_attempt(jid, user_id, *, resume=False):
    with SessionLocal() as db:
        row = locked(db, jid)
        if row is None or row.user_id != user_id:
            raise ValueError("job 不存在或无权访问")
        meta = dict(row.extra_data or {})
        old = dict(meta.get("execution") or {})
        if resume and row.status in LIVE:
            raise ValueError("作业仍在运行或启动中，请先取消，不能重复续跑")
        if resume and old.get("phase") not in ("exited", "not_started"):
            raise ValueError("原执行实例尚未确认退出，禁止重复续跑；请先取消并确认原进程停止")
        if resume:
            row.status = "pending"
            row.started_at = None
            row.completed_at = None
            row.error_message = None
            meta["token"] = mint_token()
            meta.pop("woken_at", None)
        attempt = uuid.uuid4().hex
        meta["execution"] = {
            "attempt_id": attempt,
            "phase": "preparing",
            "owner": OWNER,
            "lease_until": (now() + timedelta(seconds=180)).isoformat(),
            "started_at": now().isoformat(),
        }
        row.extra_data = meta
        flag_modified(row, "extra_data")
        db.commit()
        return attempt


def finish(jid, attempt, status, error="", *, require_owner=True):
    with SessionLocal() as db:
        row = locked(db, jid)
        if row is None or (row.extra_data or {}).get("execution", {}).get("attempt_id") != attempt:
            return
        if require_owner and row.extra_data["execution"].get("owner") != OWNER:
            return
        JobService(db).finish(jid, status, error=error)


def renew(jid, attempt):
    with SessionLocal() as db:
        row = locked(db, jid)
        if row is None:
            return False
        meta = dict(row.extra_data or {})
        execution = dict(meta.get("execution") or {})
        if execution.get("attempt_id") != attempt or execution.get("owner") != OWNER:
            return False
        execution["lease_until"] = (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
        meta["execution"] = execution
        row.extra_data = meta
        flag_modified(row, "extra_data")
        db.commit()
        return True


def claim_expired():
    """A new worker must not interrupt a live owner during rolling startup."""
    claimed = []
    with SessionLocal() as db:
        rows = db.query(Job).filter(Job.status.in_(LIVE)).with_for_update(skip_locked=True).all()
        for row in rows:
            meta = dict(row.extra_data or {})
            execution = dict(meta.get("execution") or {})
            deadline = execution.get("lease_until")
            if deadline and datetime.fromisoformat(deadline) > now():
                continue
            last = row.updated_at or row.created_at
            if not execution and last:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now() - last).total_seconds() < (300 if row.status == "pending" else 900):
                    continue
            # No process handle is ever fabricated for legacy detached jobs.
            if not execution:
                execution = {"attempt_id": uuid.uuid4().hex, "phase": "unknown"}
            execution.update(
                owner=OWNER, lease_until=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
            )
            meta["execution"] = execution
            row.extra_data = meta
            flag_modified(row, "extra_data")
            claimed.append(row.job_id)
        db.commit()
    return claimed


def take_control(jid, attempt):
    """Cancellation explicitly transfers control after callback rights are revoked."""
    return save_execution(
        jid,
        attempt,
        require_owner=False,
        owner=OWNER,
        lease_until=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
    )
