"""Owner-paired devices. The desktop device link initiates every connection; devices open no port."""
import hashlib
import hmac
import secrets
import time
import uuid
from sqlalchemy import func, select
from personal_agent.device_link import CAPABILITIES as LINK_CAPABILITIES
from personal_agent.device_link.driver import COMMAND_SECONDS
from core.db.engine import SessionLocal
from core.db.threads import run_in_session
from personal_agent.computer import receipts
from personal_agent.computer import steps as step_rules
from personal_agent.models import PersonalDevice, PersonalDeviceCommand, PersonalWorkItem

# A device that reported nothing for this long is offline. The desktop sends a heartbeat
# every 5 s with requests capped at 10 s, so one lost or slow report never crosses it.
ONLINE_WINDOW = 20
COMMAND_KEEP_SECONDS = 24 * 3600
MAX_DEVICES = 20
# What a computer may report: the actions, sessions and element reading the device link knows.
CAPABILITIES = LINK_CAPABILITIES
# How long a command may take on the computer: read and steps by driver.COMMAND_SECONDS (a batch
# includes its after-read), a single old action the old 30 s.
LEGACY_DEADLINE = 30
# A receipt that arrives just as its deadline passes is still being written; wait that long for it.
RECEIPT_GRACE = 1.0
# Between wakes the row is read again anyway, so a lost wake costs at most this long.
WAIT_SLICE = 5.0


def deadline_for(action):
    return COMMAND_SECONDS.get(action.get("type"), LEGACY_DEADLINE)


def checked_capabilities(capabilities):
    """The reported capabilities this cloud knows. A newer desktop reports ones it does not: they are
    ignored, never a reason to refuse the report (a refused heartbeat leaves the device offline)."""
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        raise ValueError("Unsupported capability")
    return sorted({item for item in capabilities if isinstance(item, str)} & CAPABILITIES)


# What a desktop says about itself with each heartbeat: its session contract (contract.contract_version)
# and app version. Bounded like the route that receives them.
LINK_FIELDS = ("contract", "app_version")
LINK_FIELD_CHARS = 64


def contract_matches(device):
    """True when the desktop speaks this cloud's session contract. One that reports none predates
    the report, so it may need an update too."""
    from hugagent_cli.contract import contract_version
    return (device.link_info or {}).get("contract") == contract_version()


def is_online(device, now=None) -> bool:
    """The one liveness rule: every check of whether a device is here goes through it."""
    return bool(device.last_seen and device.last_seen > (now or time.time()) - ONLINE_WINDOW)


def announce_departures(interval):
    """Tell open pages about computers whose heartbeat stopped during the last interval: being offline writes nothing."""
    from personal_agent.conversation.live import announce
    now = time.time()
    with SessionLocal() as db:
        users = db.scalars(select(PersonalDevice.user_id).where(
            PersonalDevice.revoked == 0,
            PersonalDevice.last_seen > now - ONLINE_WINDOW - interval,
            PersonalDevice.last_seen <= now - ONLINE_WINDOW)).all()
    announce(users)


def pair(db, user_id, name, platform, capabilities):
    if platform not in {"windows", "macos", "linux"}: raise ValueError("Unsupported platform")
    capabilities = checked_capabilities(capabilities)
    active = db.scalar(select(func.count()).select_from(PersonalDevice).where(
        PersonalDevice.user_id == user_id, PersonalDevice.revoked == 0))
    if active >= MAX_DEVICES: raise ValueError("Too many linked computers; disconnect one you no longer use")
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
        return [_listed(r) for r in rows]


def _listed(r):
    info, matches = r.link_info or {}, contract_matches(r)
    row = {"id": r.id, "name": r.name, "platform": r.platform, "capabilities": r.capabilities,
           "sessions_enabled": bool(r.sessions_enabled), "control_enabled": bool(r.control_enabled),
           "online": is_online(r), "epoch": r.epoch, "contract": info.get("contract"),
           "app_version": info.get("app_version"), "contract_matches": matches}
    if not matches:
        row["note"] = "The desktop app may need an update: its session contract differs from the cloud's."
    return row

def prune_commands(db):
    """Remove commands that ended a day ago: their receipts can hold whole screenshots.
    Run by the background maintenance loop, not on every queued command."""
    from sqlalchemy import delete
    db.execute(delete(PersonalDeviceCommand).where(PersonalDeviceCommand.deadline < time.time() - COMMAND_KEEP_SECONDS))
    db.commit()


def take_command(db, device):
    """Mark the device's next screen command sent and return it, or None. The caller commits.
    At most once: a sent command is never handed out again (screen input is not safe to repeat);
    the local receipt journal handles transport retries."""
    if not device.control_enabled: return None
    row = db.scalar(select(PersonalDeviceCommand).where(PersonalDeviceCommand.device_id == device.id,
        PersonalDeviceCommand.epoch == device.epoch, PersonalDeviceCommand.status == "queued",
        PersonalDeviceCommand.deadline > time.time()).order_by(PersonalDeviceCommand.deadline).with_for_update(skip_locked=True).limit(1))
    if row is None: return None
    task = db.get(PersonalWorkItem, row.work_item_id)
    if not task or task.status != "running" or task.run_id != row.run_id or not task.lease_until or task.lease_until < time.time():
        row.status = "revoked"
        return None
    row.status = "sent"
    return {"id": row.id, "epoch": row.epoch, "action": row.action, "deadline": row.deadline}


def poll(db, ident, token):
    """Protocol 1: the next screen command, if any."""
    device = authenticate(db, ident, token)
    device.last_seen = time.time()
    result = take_command(db, device)
    db.commit()
    return result


def claim(db, ident, token, kinds):
    """Protocol 2: the next screen command and session operation for the device's idle lanes,
    under one authentication and one transaction."""
    from personal_agent.sessions import session_queue
    device = authenticate(db, ident, token)
    db.refresh(device, with_for_update=True)
    now = time.time()
    device.last_seen = now
    result = {"command": take_command(db, device) if "command" in kinds else None,
              "operation": session_queue.take_operation(db, device, now) if "session" in kinds else None}
    db.commit()
    return result

MAX_RECEIPT = 8 * 1024 * 1024


def receipt_size_ok(result):
    import json
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RECEIPT: raise ValueError("Receipt exceeds limit")


def receivable(db, ident, token, command_id):
    """Checked before a receipt's images are stored: "completed" for a repeat, else "open"."""
    device = authenticate(db, ident, token)
    row = db.get(PersonalDeviceCommand, command_id)
    if row is None or row.device_id != ident or row.epoch != device.epoch: raise LookupError("Command not found")
    if row.status == "completed": return "completed"
    if row.status != "sent" or row.deadline < time.time(): raise ValueError("Command expired")
    return "open"


def acknowledge(db, ident, token, command_id, result):
    """Write the receipt. The receipt route has moved its images out first (receipts.offload_images)."""
    if receivable(db, ident, token, command_id) == "completed": return {"accepted": True}
    row = db.get(PersonalDeviceCommand, command_id)
    receipt_size_ok(result)
    row.result, row.status = result, "completed"
    db.commit()
    return {"accepted": True}

def _queue_command(db, task_id, user_id, ident, action):
    """Queue the command; returns (command_id, epoch), or a waiting notice."""
    device = db.scalar(select(PersonalDevice).where(PersonalDevice.id == ident, PersonalDevice.user_id == user_id, PersonalDevice.revoked == 0).with_for_update())
    if device is None: raise LookupError("Device not found")
    if not device.control_enabled:return {"waiting": True, "message": "请在本地电脑允许接管"}
    if action["type"] not in (device.capabilities or []): raise ValueError("Device cannot perform this action")
    if not is_online(device): return {"waiting": True, "message": "电脑未连接"}
    from personal_agent.tasks.guards import owned_task
    state = owned_task(task_id, user_id)
    row = PersonalDeviceCommand(run_id=state["run_id"], id="pa_command_" + uuid.uuid4().hex, device_id=ident, epoch=device.epoch,
        work_item_id=task_id, action=action, deadline=time.time() + deadline_for(action))
    db.add(row)
    db.commit()
    return row.id, device.epoch, row.deadline


def _capabilities(db, user_id, ident):
    device = db.scalar(select(PersonalDevice).where(PersonalDevice.id == ident, PersonalDevice.user_id == user_id,
                                                    PersonalDevice.revoked == 0))
    if device is None: raise LookupError("Device not found")
    return list(device.capabilities or [])

def _command_result(db, ident, command_id, epoch):
    device, row = db.get(PersonalDevice, ident), db.get(PersonalDeviceCommand, command_id)
    if device.revoked or device.epoch != epoch: raise ValueError("Device access revoked")
    return row.result if row.status == "completed" else None


def _give_up(db, command_id):
    """The wait is over. A command no device took is withdrawn: it certainly did not run."""
    row = db.scalar(select(PersonalDeviceCommand).where(PersonalDeviceCommand.id == command_id).with_for_update())
    if row.status == "completed": return row.result
    if row.status == "queued":
        row.status = "revoked"
        db.commit()
        return {"waiting": True, "not_executed": True, "message": "电脑没有接收这一步，请确认电脑在线后重试"}
    db.commit()
    return {"outcome_unknown": True, "message": "电脑未返回结果，请检查操作结果"}

async def _execute_one(task_id, user_id, ident, action):
    """Queue one command and wait for its receipt: woken by the receipt route, re-read on every wake."""
    queued = await run_in_session(_queue_command, task_id, user_id, ident, action)
    if isinstance(queued, dict): return queued
    command_id, epoch, deadline = queued
    while True:
        result = await run_in_session(_command_result, ident, command_id, epoch)
        if result is not None: return await receipts.restore_images(result)
        remaining = deadline + RECEIPT_GRACE - time.time()
        if remaining <= 0: break
        await receipts.wait(command_id, min(remaining, WAIT_SLICE))
    return await receipts.restore_images(await run_in_session(_give_up, command_id))


async def execute_command(task_id, user_id, ident, action):
    """Run a command on a paired computer. read and steps go to an older computer as the actions it knows."""
    kind = action.get("type")
    if kind in {"read", "steps"}:
        capabilities = await run_in_session(_capabilities, user_id, ident)
        if not step_rules.supports_batch(capabilities):
            if kind == "read":
                return step_rules.normalize_read(await _execute_one(task_id, user_id, ident, {"type": "observe"}))
            return await step_rules.run_legacy_steps(lambda single: _execute_one(task_id, user_id, ident, single), action)
    return await _execute_one(task_id, user_id, ident, action)

def consent(db,ident,token,enabled):
    """Legacy (protocol 1) consent report; newer desktops send it with every heartbeat."""
    device=authenticate(db,ident,token)
    db.refresh(device,with_for_update=True)
    if device.revoked:raise LookupError("Device unavailable")
    _set_control(db, device, enabled)
    device.last_seen=time.time();db.commit()
    return {"control_enabled":bool(device.control_enabled),"epoch":device.epoch}


def _set_control(db, device, enabled):
    """Withdrawing control revokes every command not yet answered, under a new epoch."""
    from sqlalchemy import update
    if not enabled and device.control_enabled:
        device.epoch += 1
        db.execute(update(PersonalDeviceCommand).where(PersonalDeviceCommand.device_id == device.id,
            PersonalDeviceCommand.status.in_(["queued", "sent"])).values(status="revoked"))
    device.control_enabled = int(enabled)


def heartbeat(db, ident, token, control_enabled, sessions_enabled, capabilities=None, contract=None, app_version=None,
              changed_chats=None, chat_list_changed=False):
    """The device is here, with the consent it holds locally right now.

    Restating an unchanged consent changes nothing but the last-seen time. A consent that
    was withdrawn revokes the pending work it covered, exactly as the consent routes do.
    A desktop that reports its capabilities (A.5) replaces the set it was paired with:
    an update can add the batch commands, and losing the elements driver removes ui.elements.
    A desktop that reports its contract and app version has them stored for device.list.
    One with session consent may report which of its own chats changed; open pages are told.
    """
    from personal_agent.sessions import session_queue
    reported = None if capabilities is None else checked_capabilities(capabilities)
    device = authenticate(db, ident, token)
    db.refresh(device, with_for_update=True)
    if device.revoked: raise LookupError("Device unavailable")
    if reported is not None and reported != sorted(device.capabilities or []): device.capabilities = reported
    reported_link = {key: str(value)[:LINK_FIELD_CHARS] for key, value in
                     (("contract", contract), ("app_version", app_version)) if value is not None}
    if reported_link and {**(device.link_info or {}), **reported_link} != (device.link_info or {}):
        device.link_info = {**(device.link_info or {}), **reported_link}
    _set_control(db, device, control_enabled)
    # A device paired without session support cannot turn it on; that is not an error here.
    session_queue.set_sessions(db, device, sessions_enabled and "hugagent.sessions" in device.capabilities)
    device.last_seen = time.time()
    db.commit()
    if device.sessions_enabled and (changed_chats or chat_list_changed):
        from personal_agent.conversation.live import announce
        topic = "device:" + device.id
        announce([device.user_id], ([topic] if chat_list_changed else [])
                 + [topic + ":" + str(chat)[:128] for chat in (changed_chats or [])[:100]])
    return {"protocol": 2, "server_time": device.last_seen, "control_enabled": bool(device.control_enabled),
            "sessions_enabled": bool(device.sessions_enabled), "epoch": device.epoch,
            "session_epoch": device.session_epoch}
