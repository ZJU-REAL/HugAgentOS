"""Owner-only durable operations. Sent writes are never redelivered.

A sent read whose answer has not come back after REDELIVER_AFTER is handed out again:
the reply carrying it may have been lost on the way, and reading twice changes nothing.
The device's own journal still refuses an id it has already started.
"""

import json
import time
import uuid
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from hugagent_cli.contract import validate
from personal_agent.computer.devices import authenticate, is_online
from hugagent_cli.contract import READS
from personal_agent.models import PersonalDevice, PersonalSessionOperation as Operation, PersonalWorkItem

REDELIVER_AFTER = 15
# Finished reads are kept this long, then removed.
KEEP_SECONDS = 24 * 3600

def public(row):
    status = row.status
    if status in {"queued", "sent"} and row.deadline < time.time():
        status = "expired" if status == "queued" else "outcome_unknown"
    return {
        "id": row.id,
        "target": row.target,
        "operation": row.operation,
        "status": status,
        "result": row.result,
        "deadline": row.deadline,
    }

def view(db, uid, ident, task_id=None):
    row = db.get(Operation, ident)
    if row is None or row.user_id != uid or (task_id and row.work_item_id != task_id):
        raise LookupError("Operation not found")
    return public(row)

def submit(
    db, uid, target, operation, args, request_key, task_id=None, run_id=None, task_scope=None
):
    from core.db.models import UserShadow

    db.scalar(select(UserShadow).where(UserShadow.user_id == uid).with_for_update())
    args = validate(operation, args)
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100:
        raise ValueError("Invalid request key")
    old = db.scalar(
        select(Operation).where(Operation.user_id == uid, Operation.request_key == request_key)
    )
    if old:
        if (old.target, old.operation, old.args, old.work_item_id) != (
            target,
            operation,
            args,
            task_id,
        ):
            raise ValueError("Request key already has different arguments")
        if not (old.status == "queued" and old.deadline < time.time()):
            return public(old)
        # Expired while still queued: no device ever saw it, so it certainly did not run.
        # Asking again queues it again, instead of answering "expired" forever.
        db.delete(old)
        db.flush()
    device = None
    if target != "cloud":
        if not target.startswith("device:"):
            raise ValueError("Invalid target")
        device = db.scalar(
            select(PersonalDevice)
            .where(PersonalDevice.id == target[7:], PersonalDevice.user_id == uid)
            .with_for_update()
        )
        if not device or device.revoked:
            raise LookupError("Device not found")
        if not device.sessions_enabled or "hugagent.sessions" not in device.capabilities:
            raise ValueError("Enable session management on the local device")
        if not is_online(device):
            raise ValueError("Device offline")
    if (
        db.scalar(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.user_id == uid,
                Operation.status.in_(["queued", "sent"]),
                Operation.deadline > time.time(),
            )
        )
        >= 50
    ):
        raise ValueError("Session operation queue full")
    task = None
    if task_id:
        task = db.scalar(
            select(PersonalWorkItem).where(PersonalWorkItem.id == task_id).with_for_update()
        )
        if (
            not task
            or task.status != "running"
            or task.run_id != run_id
            or (
                task_scope
                and task.owner != task_scope["owner"]
            )
        ):
            raise ValueError("Task execution changed")
    row = Operation(
        id="pa_session_" + uuid.uuid4().hex,
        user_id=uid,
        target=target,
        device_id=device.id if device else None,
        epoch=device.session_epoch if device else 1,
        request_key=request_key,
        operation=operation,
        args=args,
        work_item_id=task_id,
        run_id=run_id,
        task_owner=task.owner if task else None,
        deadline=time.time() + 60,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not db.scalar(
            select(Operation.id).where(
                Operation.user_id == uid, Operation.request_key == request_key
            )
        ):
            raise
        return submit(db, uid, target, operation, args, request_key, task_id, run_id, task_scope)
    return public(row)

def prune(db):
    """Remove reads that ended long ago: their results can hold whole chat histories. Writes stay:
    their row is the receipt a task finds instead of repeating the write. Run by the background
    maintenance loop, not on every submit."""
    from sqlalchemy import delete
    db.execute(
        delete(Operation).where(
            Operation.operation.in_(sorted(READS)),
            Operation.deadline < time.time() - KEEP_SECONDS,
        )
    )
    db.commit()


def active_task(db, row):
    if not row.work_item_id:
        return True
    task = db.get(PersonalWorkItem, row.work_item_id)
    return bool(
        task
        and task.status == "running"
        and task.run_id == row.run_id
        and task.owner == row.task_owner
        and task.lease_until
        and task.lease_until > time.time()
    )

def poll(db, ident, token):
    """Protocol 1: the next session operation, if any."""
    device = authenticate(db, ident, token)
    db.refresh(device, with_for_update=True)
    now = time.time()
    device.last_seen = now
    result = take_operation(db, device, now)
    db.commit()
    return result


def take_operation(db, device, now):
    """Mark the device's next session operation sent and return it, or None. The caller commits."""
    if not device.sessions_enabled:
        return None
    lost_read = (
        (Operation.status == "sent")
        & Operation.operation.in_(sorted(READS))
        & ((Operation.claimed_at.is_(None)) | (Operation.claimed_at < now - REDELIVER_AFTER))
    )
    row = db.scalar(
        select(Operation)
        .where(
            Operation.device_id == device.id,
            Operation.epoch == device.session_epoch,
            (Operation.status == "queued") | lost_read,
            Operation.deadline > now,
        )
        .order_by(Operation.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    if not active_task(db, row):
        row.status = "revoked"
        return None
    row.status, row.claimed_at = "sent", now
    return {**public(row), "args": row.args, "epoch": row.epoch}


def set_sessions(db, device, enabled):
    """Withdrawing session consent revokes every operation not yet answered, under a new epoch."""
    if not enabled and device.sessions_enabled:
        device.session_epoch += 1
        db.execute(
            update(Operation)
            .where(Operation.device_id == device.id, Operation.status.in_(["queued", "sent"]))
            .values(status="revoked")
        )
    device.sessions_enabled = int(enabled)

def acknowledge(db, ident, token, operation_id, result):
    device = authenticate(db, ident, token)
    db.refresh(device, with_for_update=True)
    row = db.get(Operation, operation_id)
    if (
        row is None
        or row.device_id != ident
        or row.epoch != device.session_epoch
        or not device.sessions_enabled
    ):
        raise LookupError("Operation not found")
    db.refresh(row, with_for_update=True)
    if row.status == "completed":
        if row.result != result:
            raise ValueError("Receipt cannot change")
        return {"accepted": True}
    if row.status != "sent" or row.deadline < time.time() or not active_task(db, row):
        raise ValueError("Operation expired or revoked")
    # Measured as UTF-8, as the device measures it: escaped CJK would count double.
    if len(json.dumps(result, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("Receipt exceeds limit")
    row.result = result
    row.status = "completed"
    db.commit()
    return {"accepted": True}

def consent(db, ident, token, enabled):
    device = authenticate(db, ident, token)
    db.refresh(device, with_for_update=True)
    if "hugagent.sessions" not in device.capabilities:
        raise ValueError("Session capability not paired")
    set_sessions(db, device, enabled)
    device.last_seen = time.time()
    db.commit()
    return {"sessions_enabled": bool(device.sessions_enabled), "epoch": device.session_epoch}

def check(db, ident, token, operation_id):
    device = authenticate(db, ident, token)
    row = db.get(Operation, operation_id)
    if (
        not row
        or row.device_id != ident
        or row.epoch != device.session_epoch
        or not device.sessions_enabled
        or row.status != "sent"
        or row.deadline < time.time()
        or not active_task(db, row)
    ):
        raise LookupError("Operation revoked")
    return {"permitted": True}
