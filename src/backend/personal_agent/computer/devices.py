"""Owner-paired devices. The desktop device link initiates every connection; devices open no port."""
import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from sqlalchemy import select
from personal_agent.device_link import CAPABILITIES
from core.db.engine import SessionLocal
from core.db.threads import run_in_session
from personal_agent.models import PersonalDevice, PersonalDeviceCommand, PersonalWorkItem

def pair(db, user_id, name, platform, capabilities):
    if platform not in {"windows", "macos", "linux"}: raise ValueError("Unsupported platform")
    if not set(capabilities) <= CAPABILITIES: raise ValueError("Unsupported capability")
    token = secrets.token_urlsafe(48)
    row = PersonalDevice(id="pa_device_" + uuid.uuid4().hex, user_id=user_id, name=name,
                         platform=platform, capabilities=capabilities,
                         secret_hash=hashlib.sha256(token.encode()).hexdigest())
    db.add(row)
    db.commit()
    return {"id": row.id, "token": token, "epoch": row.epoch}

def authenticate(db, ident, token):
    row = db.get(PersonalDevice, ident)
    if row is None or row.revoked or not hmac.compare_digest(row.secret_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise LookupError("Device unavailable")
    return row

def list_devices(user_id):
    with SessionLocal() as db:
        rows = db.scalars(select(PersonalDevice).where(PersonalDevice.user_id == user_id, PersonalDevice.revoked == 0)).all()
        return [{"id": r.id, "name": r.name, "platform": r.platform, "capabilities": r.capabilities,
                 "sessions_enabled":bool(r.sessions_enabled), "control_enabled":bool(r.control_enabled), "online": bool(r.last_seen and r.last_seen > time.time() - 20), "epoch": r.epoch} for r in rows]

def poll(db, ident, token):
    device = authenticate(db, ident, token)
    device.last_seen = time.time()
    if not device.control_enabled:
        db.commit();return None
    # A sent command is never dispatched again. The local receipt journal handles transport retries.
    row = db.scalar(select(PersonalDeviceCommand).where(PersonalDeviceCommand.device_id == ident,
        PersonalDeviceCommand.epoch == device.epoch, PersonalDeviceCommand.status == "queued",
        PersonalDeviceCommand.deadline > time.time()).order_by(PersonalDeviceCommand.deadline).with_for_update(skip_locked=True).limit(1))
    result = None
    if row:
        task = db.get(PersonalWorkItem, row.work_item_id)
        if not task or task.status != "running" or task.run_id != row.run_id or not task.lease_until or task.lease_until < time.time():
            row.status = "revoked"
            db.commit()
            return None
        row.status = "sent"
        result = {"id": row.id, "epoch": row.epoch, "action": row.action, "deadline": row.deadline}
    db.commit()
    return result

def acknowledge(db, ident, token, command_id, result):
    device = authenticate(db, ident, token)
    row = db.get(PersonalDeviceCommand, command_id)
    if row is None or row.device_id != ident or row.epoch != device.epoch: raise LookupError("Command not found")
    if row.status == "completed": return {"accepted": True}
    if row.status != "sent" or row.deadline < time.time(): raise ValueError("Command expired")
    import json
    if len(json.dumps(result)) > 8 * 1024 * 1024: raise ValueError("Receipt exceeds limit")
    row.result, row.status = result, "completed"
    db.commit()
    return {"accepted": True}

def _queue_command(db, task_id, user_id, ident, action):
    """Queue the command; returns (command_id, epoch), or a waiting notice."""
    device = db.scalar(select(PersonalDevice).where(PersonalDevice.id == ident, PersonalDevice.user_id == user_id, PersonalDevice.revoked == 0).with_for_update())
    if device is None: raise LookupError("Device not found")
    if not device.control_enabled:return {"waiting": True, "message": "请在本地电脑允许接管"}
    if action["type"] not in device.capabilities: raise ValueError("Device cannot perform this action")
    if not device.last_seen or device.last_seen < time.time() - 20: return {"waiting": True, "message": "电脑未连接"}
    from personal_agent.tasks.guards import owned_task
    state = owned_task(task_id, user_id)
    row = PersonalDeviceCommand(run_id=state["run_id"], id="pa_command_" + uuid.uuid4().hex, device_id=ident, epoch=device.epoch,
        work_item_id=task_id, action=action, deadline=time.time() + 30)
    db.add(row)
    db.commit()
    return row.id, device.epoch

def _command_result(db, ident, command_id, epoch):
    device, row = db.get(PersonalDevice, ident), db.get(PersonalDeviceCommand, command_id)
    if device.revoked or device.epoch != epoch: raise ValueError("Device access revoked")
    return row.result if row.status == "completed" else None

async def execute_command(task_id, user_id, ident, action):
    queued = await run_in_session(_queue_command, task_id, user_id, ident, action)
    if isinstance(queued, dict): return queued
    command_id, epoch = queued
    for _ in range(120):
        await asyncio.sleep(.25)
        result = await run_in_session(_command_result, ident, command_id, epoch)
        if result is not None: return result
    return {"outcome_unknown": True, "message": "电脑未返回结果，请检查操作结果"}

def consent(db,ident,token,enabled):
    from sqlalchemy import update
    device=authenticate(db,ident,token)
    db.refresh(device,with_for_update=True)
    if device.revoked:raise LookupError("Device unavailable")
    if not enabled and device.control_enabled:
        device.epoch+=1
        db.execute(update(PersonalDeviceCommand).where(PersonalDeviceCommand.device_id==ident,
            PersonalDeviceCommand.status.in_(["queued","sent"])).values(status="revoked"))
    device.control_enabled=int(enabled);device.last_seen=time.time();db.commit()
    return {"control_enabled":bool(device.control_enabled),"epoch":device.epoch}
