"""Bounded private screenshot staging. Observation never creates a user-visible artifact."""
import personal_agent.tasks.guards as _pa_guards
import asyncio
import base64
import io
import json
import re
import time
import uuid

from PIL import Image
from sqlalchemy import select

from personal_agent.delivery.delivery import MAX_BYTES, active_task

TTL = 600
KEEP = 4
REF = re.compile(r"screen_[0-9a-f]{30,64}")

def device_scope(db, uid, target, epoch=None):
    if target == "cloud": return
    from personal_agent.models import PersonalDevice
    device = db.scalar(select(PersonalDevice).where(
        PersonalDevice.id == target.removeprefix("device:")
    ).with_for_update()) if target.startswith("device:") else None
    if (not device or device.user_id != uid or device.revoked or not device.control_enabled
            or "observe" not in (device.capabilities or []) or (epoch is not None and device.epoch != epoch)):
        raise PermissionError("Screenshot device permission changed")
    return device.epoch

def prepare(ident, uid, target):
    from personal_agent.tools import tools
    from personal_agent.delivery import delivery
    state = _pa_guards.owned_task(ident, uid)
    with delivery.SessionLocal() as db:
        active_task(db, ident, uid, state)
        state["resource_target"] = target
        state["resource_epoch"] = device_scope(db, uid, target)
    return state

def validate_scope(ident, uid, state):
    from personal_agent.delivery import delivery
    with delivery.SessionLocal() as db:
        active_task(db, ident, uid, state)
        device_scope(db, uid, state["resource_target"], state.get("resource_epoch"))

def decode_png(data):
    if not isinstance(data, str) or len(data) > (MAX_BYTES + 2) // 3 * 4:
        raise ValueError("Screenshot exceeds 20 MiB")
    content = base64.b64decode(data, validate=True)
    if len(content) > MAX_BYTES: raise ValueError("Screenshot exceeds 20 MiB")
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "PNG" or image.width * image.height > 32_000_000:
            raise ValueError("Screenshot must be a bounded PNG image")
        image.verify()
    return content

def prefix(ident, uid):
    return "pa:delivery-resource:" + uid + ":" + ident + ":"

async def stage(ident, uid, state, data):
    from core.infra.ephemeral import get_ephemeral_state
    await asyncio.to_thread(decode_png, data)
    await asyncio.to_thread(validate_scope, ident, uid, state)
    cache = get_ephemeral_state()
    namespace = prefix(ident, uid)
    token = uuid.uuid4().hex
    lock = "pa:delivery-resource-lock:" + ident
    if not await cache.hold(lock, token, ttl=10):
        raise RuntimeError("Screenshot staging is busy; observe again")
    try:
        keys = sorted(await cache.keys(namespace))
        if len(keys) >= KEEP: await cache.drop(*keys[:len(keys) - KEEP + 1])
        ref = "screen_" + format(time.time_ns(), "x") + uuid.uuid4().hex[:16]
        value = {key: state[key] for key in ("run_id", "owner", "generation",
                                             "resource_target", "resource_epoch")}
        value["image"] = data
        await cache.put(namespace + ref, json.dumps(value), ttl=TTL)
        return ref
    finally:
        await cache.release(lock, token)

async def resolve(ident, uid, state, ref):
    if not REF.fullmatch(ref):
        raise ValueError("Invalid screenshot resource_ref; use pa_computer_observe")
    from core.infra.ephemeral import get_ephemeral_state
    value = await get_ephemeral_state().get(prefix(ident, uid) + ref)
    if not value:
        raise ValueError("Screenshot resource expired or unavailable. Observe again, then pa_deliver with a new key. Do not use Bash.")
    resource = json.loads(value)
    if any(resource.get(key) != state[key] for key in ("run_id", "owner", "generation")):
        raise PermissionError("Screenshot belongs to another execution")
    state["resource_target"], state["resource_epoch"] = resource["resource_target"], resource["resource_epoch"]
    await asyncio.to_thread(validate_scope, ident, uid, state)
    return await asyncio.to_thread(decode_png, resource["image"])
