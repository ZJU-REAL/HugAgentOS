"""Own run leases and journal-bound creation, fencing and terminal transitions."""

from __future__ import annotations

import asyncio
import typing as _typing
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import core.db.engine as engine
import core.db.models as models
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.runs.journal as run_journal
from core.db.models import ChatRun
from core.infra.logging import get_logger
from core.runs.journal import RunJournal

logger = get_logger(__name__)


def journal() -> RunJournal:
    # Resolve SessionLocal at call time so tests and alternate runtime profiles
    # can replace the factory without leaving the journal bound to another DB.
    return run_journal.RunJournal(engine.SessionLocal)


def new_worker_owner(run_id: str) -> str:
    return f"{settings._WORKER_INSTANCE_ID}:{run_id}:{uuid.uuid4().hex[:12]}"


async def lease_heartbeat(
    run_id: str,
    owner: str,
    worker: asyncio.Task,
    lease_lost: asyncio.Event,
) -> None:
    """Renew ownership and fence the local coroutine immediately on lease loss."""
    interval = settings._RUN_LEASE_HEARTBEAT_SEC
    try:
        run = await asyncio.to_thread(runtime.get_run, run_id)
        payload = (run.request_payload or {}) if run else {}
        if payload.get("api_key_id") or payload.get("agent_api_scope"):
            # A revoke handled by another worker must also stop a silent model call.
            interval = min(interval, 1.0)
        while True:
            await asyncio.sleep(interval)
            # ``renew`` is a synchronous UPDATE on chat_runs. Run it off the
            # event loop: whenever that row is locked by another transaction
            # this call waits inside the driver, and waiting *on the loop*
            # freezes every coroutine in the process — including the one
            # holding the lock, which then can never commit and release it.
            # A blocked heartbeat must only cost this run its lease, never the
            # whole backend.
            renewed = await asyncio.to_thread(
                journal().renew,
                run_id,
                owner=owner,
                lease_seconds=settings.RUN_LEASE_SECONDS,
            )
            if not renewed:
                logger.warning("chat_run_lease_lost", run_id=run_id, owner=owner)
                lease_lost.set()
                worker.cancel()
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # DB/driver failures must fail closed
        logger.error(
            "chat_run_lease_renew_failed",
            run_id=run_id,
            owner=owner,
            error=str(exc),
            exc_info=True,
        )
        lease_lost.set()
        worker.cancel()


def _update_run_status(run_id: str, **fields: Any) -> None:
    """Single-shot UPDATE on chat_runs; isolated session so it doesn't pollute caller's txn."""
    if not fields:
        return
    if fields.get("status") in settings._TERMINAL_STATUSES:
        fields.setdefault("writer_slot", None)
    with engine.SessionLocal() as db:
        affected = (
            db.query(models.ChatRun)
            .filter(models.ChatRun.run_id == run_id)
            .update(fields, synchronize_session=False)
        )
        if not affected:
            logger.warning("chat_run_update_missing", run_id=run_id, fields=list(fields.keys()))
        db.commit()


def _claim_run_execution(run_id: str) -> Optional[datetime]:
    """Fence duplicate initial workers before acquiring the durable lease.

    Returns the instant the run was marked running, or None when another worker
    got there first. The caller needs that instant for the first frame, and
    handing it back keeps the send path free of a second read of the same row.
    """

    started_at = clock._utcnow()
    with engine.SessionLocal() as db:
        affected = (
            db.query(models.ChatRun)
            .filter(models.ChatRun.run_id == run_id, models.ChatRun.status == "pending")
            .update(
                {"status": "running", "started_at": started_at},
                synchronize_session=False,
            )
        )
        db.commit()
    return started_at if affected else None


def _finalize_run(
    run_id: str,
    *,
    require_expired_lease: bool = False,
    **fields: Any,
) -> bool:
    """CAS variant of writing the terminal state: only updates while the run is still live.

    Prevents the race where "the reaper / cancel has already moved the run to a
    terminal state, and the worker's late completed/failed overwrites it back" —
    the side that loses the race abandons its write and returns False.
    """
    fields.setdefault("lease_owner", None)
    fields.setdefault("lease_expires_at", None)
    if fields.get("error_message") and not fields.get("failure_reason"):
        fields["failure_reason"] = fields["error_message"]
    if fields.get("status") in settings._TERMINAL_STATUSES:
        fields.setdefault("writer_slot", None)
    with engine.SessionLocal() as db:
        query = db.query(models.ChatRun).filter(
            models.ChatRun.run_id == run_id,
            models.ChatRun.status.in_(settings.LIVE_STATUSES),
        )
        if require_expired_lease:
            from sqlalchemy import or_

            now = clock._utcnow()
            query = query.filter(
                or_(
                    models.ChatRun.lease_owner.is_(None),
                    models.ChatRun.lease_expires_at.is_(None),
                    models.ChatRun.lease_expires_at <= now,
                )
            )
        affected = query.update(fields, synchronize_session=False)
        db.commit()
    if not affected:
        logger.info("chat_run_finalize_skipped", run_id=run_id, fields=list(fields.keys()))
    return bool(affected)


def _request_run_stop(
    run_id: str,
    *,
    status: settings.RunStatus,
    error_message: Optional[str] = None,
    require_expired_lease: bool = False,
) -> bool:
    """Write an external terminal verdict while retaining the writer fence."""

    if status not in settings._TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status}")
    fields: _typing.Dict[str, _typing.Any] = {"status": status, "completed_at": clock._utcnow()}
    if error_message:
        fields["error_message"] = error_message[:1000]
        fields["failure_reason"] = error_message[:1000]
    with engine.SessionLocal() as db:
        query = db.query(models.ChatRun).filter(
            models.ChatRun.run_id == run_id,
            models.ChatRun.status.in_(settings.LIVE_STATUSES),
        )
        if require_expired_lease:
            from sqlalchemy import or_

            now = clock._utcnow()
            query = query.filter(
                or_(
                    models.ChatRun.lease_owner.is_(None),
                    models.ChatRun.lease_expires_at.is_(None),
                    models.ChatRun.lease_expires_at <= now,
                )
            )
        affected = query.update(fields, synchronize_session=False)
        db.commit()
    return bool(affected)


def _request_run_cancel(run_id: str) -> bool:
    return _request_run_stop(run_id, status="cancelled")


def _acknowledge_terminal_writer(run_id: str) -> bool:
    """Release the writer fence only after the owning worker has stopped writing."""

    with engine.SessionLocal() as db:
        affected = (
            db.query(models.ChatRun)
            .filter(
                models.ChatRun.run_id == run_id,
                models.ChatRun.status.in_(settings._TERMINAL_STATUSES),
                models.ChatRun.writer_slot.isnot(None),
            )
            .update({"writer_slot": None}, synchronize_session=False)
        )
        db.commit()
    return bool(affected)


def _acknowledge_never_started_terminal_writer(run_id: str) -> bool:
    """Release a terminal fence only when no worker ever claimed the run.

    A duplicate worker can lose the pending-to-running claim while the real
    worker is active, then observe a concurrent cancellation.  It must not
    release the real worker's fence in that case.  ``started_at IS NULL`` is
    the durable proof that there is no owning worker to wait for.
    """

    with engine.SessionLocal() as db:
        affected = (
            db.query(models.ChatRun)
            .filter(
                models.ChatRun.run_id == run_id,
                models.ChatRun.status.in_(settings._TERMINAL_STATUSES),
                models.ChatRun.writer_slot.isnot(None),
                models.ChatRun.started_at.is_(None),
            )
            .update({"writer_slot": None}, synchronize_session=False)
        )
        db.commit()
    return bool(affected)


def create_run_record(
    *,
    chat_id: str,
    user_id: str,
    request_payload: Dict[str, Any],
    recovery_snapshot: Optional[Dict[str, Any]] = None,
    on_accept=None,
) -> ChatRun:
    """Durably accept a run before any worker task can be spawned."""
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    return journal().accept(
        run_id=run_id,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=writer._json_safe(request_payload),
        recovery_snapshot=writer._json_safe(recovery_snapshot or {}),
        on_accept=on_accept,
    )


def cancelled_by_user(run_id: str) -> bool:
    """租约被清掉有两种可能：别的 worker 接管了恢复，或者用户按了停止。

    cancel_run 会先把行写成终态 cancelled 再清租约，之后不会再有人接手这一轮 ——
    被围栏的 worker 若也闭嘴，用户已经看到的半截回答就没人落库了。
    """
    try:
        current = runtime.get_run(run_id)
    except Exception:  # noqa: BLE001 - 读不到就按"别人接管"处理，宁可不写
        return False
    return current is not None and current.status == "cancelled"
