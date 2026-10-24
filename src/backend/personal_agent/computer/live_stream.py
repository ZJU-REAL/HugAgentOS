"""Live cloud screen: VNC from the permanent computer, relayed to the owner's browser.

Watching never sends input. The relay drops keyboard, pointer and clipboard messages
unless the viewer holds the control session from human_control.begin.
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import struct

from core.infra.ephemeral import get_ephemeral_state
from personal_agent.computer.human_control import keys, owner, refresh
from personal_agent.computer.sandbox import STREAM_PORT, controller

TICKET_TTL = 30
# How quickly an open control stream notices that control ended elsewhere.
CONTROL_CHECK_SEC = 2
MAX_CLIPBOARD = 1024 * 1024
# RFB client messages that change the desktop: KeyEvent, PointerEvent, ClientCutText.
INPUT = {4, 5, 6}

def stream_token(execd_secret):
    """The container derives the same value from its execd token at start."""
    return hmac.new(execd_secret.encode(), b"pa-desktop-stream", hashlib.sha256).hexdigest()

async def ticket(svc, token=None):
    """A single-use ticket for one WebSocket. A control ticket needs the live control session."""
    agent = owner(svc)
    if token:
        _, session = keys(agent.id, token)
        if await get_ephemeral_state().get(session) != svc.user_id: raise LookupError("Control session expired")
    value = secrets.token_urlsafe(32)
    await get_ephemeral_state().put("pa:stream:" + value, json.dumps({"user_id": svc.user_id, "agent_id": agent.id, "token": token or ""}), ttl=TICKET_TTL)
    return {"ticket": value}

class ClientFilter:
    """Split the browser's RFB byte stream into messages and drop input while watching."""

    def __init__(self, control):
        self.control = control
        self.buffer = bytearray()
        self.handshake = None

    def feed(self, data):
        self.buffer += data
        out = bytearray()
        if self.handshake is None:
            if len(self.buffer) < 12: return b""
            # Version, then the security choice (not in 3.3), then ClientInit.
            self.handshake = 13 if bytes(self.buffer[:12]) == b"RFB 003.003\n" else 14
        if self.handshake:
            count = min(self.handshake, len(self.buffer))
            out += self.buffer[:count]
            del self.buffer[:count]
            self.handshake -= count
        while self.buffer:
            size = self.size()
            if size is None or len(self.buffer) < size: break
            if self.control or self.buffer[0] not in INPUT: out += self.buffer[:size]
            del self.buffer[:size]
        if len(self.buffer) > MAX_CLIPBOARD + 16: raise ValueError("Message too large")
        return bytes(out)

    def size(self):
        kind, data = self.buffer[0], self.buffer
        fixed = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10}
        if kind in fixed: return fixed[kind]
        if kind == 2: return 4 + 4 * struct.unpack(">H", data[2:4])[0] if len(data) >= 4 else None
        if kind == 6:
            if len(data) < 8: return None
            length = abs(struct.unpack(">i", data[4:8])[0])
            if length > MAX_CLIPBOARD: raise ValueError("Clipboard too large")
            return 8 + length
        if kind == 248: return 9 + data[8] if len(data) >= 9 else None
        raise ValueError("Unsupported RFB message")

def upstream(sandbox_id, execd_secret):
    """URL and headers for the computer's stream service, through the sandbox controller proxy."""
    domain, key = controller()
    scheme = "wss" if domain.startswith("https://") else "ws"
    url = scheme + "://" + domain.split("://", 1)[-1].rstrip("/") + "/sandboxes/" + sandbox_id + "/proxy/" + str(STREAM_PORT)
    return url, {"OPEN-SANDBOX-API-KEY": key, "X-PA-Stream-Token": stream_token(execd_secret)}


def _computer_target(db, grant):
    """``(sandbox_id, secret, agent_id)`` for a ready computer the grant names, else None."""
    from core.infra.crypto import decrypt_secret
    from personal_agent.models import PersonalAgent

    agent = db.get(PersonalAgent, grant["agent_id"])
    if agent is None or agent.user_id != grant["user_id"] or agent.computer_status != "ready" or not agent.sandbox_id or not agent.execd_secret_enc:
        return None
    return agent.sandbox_id, decrypt_secret(agent.execd_secret_enc), agent.id


async def relay(websocket, value):
    import websockets
    from core.db.threads import run_in_session

    state = get_ephemeral_state()
    raw = await state.take("pa:stream:" + value) if value else None
    if not raw:
        await websocket.close(4401)
        return
    grant = json.loads(raw)
    target = await run_in_session(_computer_target, grant)
    if target is None:
        await websocket.close(4404)
        return
    sandbox_id, secret, agent_id = target
    control = bool(grant["token"])
    url, headers = upstream(sandbox_id, secret)
    try:
        remote = await websockets.connect(url, additional_headers=headers, compression=None, max_size=None, open_timeout=10)
    except Exception:
        await websocket.close(4502)
        return
    await websocket.accept()
    stream_filter = ClientFilter(control)

    async def from_browser():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect": return
            data = message.get("bytes") or (message.get("text") or "").encode()
            out = stream_filter.feed(data)
            if out: await remote.send(out)

    async def to_browser():
        async for data in remote:
            await websocket.send_bytes(data if isinstance(data, bytes) else data.encode())

    async def keep_control():
        # Control lasts while the session lives. Ending control elsewhere closes this stream.
        while await refresh(agent_id, grant["user_id"], grant["token"]):
            await asyncio.sleep(CONTROL_CHECK_SEC)

    tasks = [asyncio.create_task(from_browser()), asyncio.create_task(to_browser())]
    if control: tasks.append(asyncio.create_task(keep_control()))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks: task.cancel()
        await remote.close()
        try: await websocket.close()
        except RuntimeError: pass
