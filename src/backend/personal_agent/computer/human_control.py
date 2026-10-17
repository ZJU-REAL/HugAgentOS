"""Owner-only desktop takeover. Input itself travels over the live stream (live_stream.py)."""
import asyncio
import base64
import io
import json
import secrets
import time

from core.infra.ephemeral import get_ephemeral_state
from personal_agent.computer.computer import LEASE_TTL, cloud_driver, desktop_lease

PREVIEW_SIZE = (360, 225)
PREVIEW_TTL = 15

def owner(svc):
    agent = svc.identity.agent()
    if not agent or agent.computer_status != "ready": raise ValueError("Cloud computer is not ready")
    return agent

def keys(agent_id, token):
    """The desktop lease shared with the agent, and this owner's control session."""
    return desktop_lease(agent_id), "pa:human:" + agent_id + ":" + token

async def begin(svc):
    agent = owner(svc)
    token = secrets.token_urlsafe(32)
    lease, session = keys(agent.id, token)
    state = get_ephemeral_state()
    if not await state.hold(lease, token, ttl=LEASE_TTL): raise ValueError("Cloud computer is currently in use")
    await state.put(session, agent.user_id, ttl=LEASE_TTL)
    return {"token": token}

async def refresh(agent_id, user_id, token):
    """Extend control while the owner's stream is open. False when control has ended."""
    lease, session = keys(agent_id, token)
    state = get_ephemeral_state()
    return (await state.get(session) == user_id and await state.hold(lease, token, ttl=LEASE_TTL)
            and await state.compare_exchange(session, user_id, user_id, ttl=LEASE_TTL))

async def end(svc, token):
    agent = svc.identity.agent()
    if not agent: raise LookupError("Personal Agent not found")
    lease, session = keys(agent.id, token)
    state = get_ephemeral_state()
    # Only the session owner can end it: another user's token names a session they do not hold.
    if await state.get(session) == svc.user_id:
        await state.drop(session)
        await state.release(lease, token)
    return {"released": True}

def thumbnail(image_base64):
    from PIL import Image
    image = Image.open(io.BytesIO(base64.b64decode(image_base64))).convert("RGB")
    image.thumbnail(PREVIEW_SIZE)
    data = io.BytesIO()
    image.save(data, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode()

async def preview(svc):
    """A cached thumbnail. Read-only: it never takes the desktop lease, so the agent keeps working."""
    agent = owner(svc)
    state = get_ephemeral_state()
    key = "pa:preview:" + agent.id
    cached = await state.get(key)
    if cached: return json.loads(cached)
    frame = await cloud_driver({"box": agent.sandbox_session_id}, svc.user_id, {"type": "observe"})
    result = {"image": await asyncio.to_thread(thumbnail, frame["image_base64"]), "taken_at": time.time()}
    await state.put(key, json.dumps(result), ttl=PREVIEW_TTL)
    return result
