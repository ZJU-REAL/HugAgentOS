"""Authenticated resource stream with bounded packet parsing and no action replay."""
import asyncio
import json
import logging
import struct
import uuid
import httpx
from fastapi import HTTPException, WebSocket
from core.db.threads import run_in_session
from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.authentication import validate as validate_authentication
from core.capabilities.plugins.resources.transport import RuntimeTransport

MAX_PACKET = 8 * 1024 * 1024

async def packets(stream):
    buffer = bytearray()
    async for chunk in stream:
        buffer.extend(chunk)
        while len(buffer) >= 4:
            size = struct.unpack(">I", buffer[:4])[0]
            if size > 65536:
                raise ValueError("invalid_frame_header")
            if len(buffer) < 4 + size:
                break
            header = json.loads(buffer[4:4 + size])
            data_size = header.get("bytes", 0)
            if not isinstance(data_size, int) or data_size < 0 or data_size > MAX_PACKET:
                raise ValueError("frame_too_large")
            total = 4 + size + data_size
            if len(buffer) < total:
                break
            yield bytes(buffer[:total])
            del buffer[:total]
        if len(buffer) > MAX_PACKET + 65540:
            raise ValueError("frame_buffer_overflow")

def _consume_ticket(db, ticket, resource_id, origin):
    return store.consume(db, ticket, resource_id, origin, with_authentication=True)


def _authorized_target(db, resource_id, user_id):
    row = service.authorized_row(db, resource_id, user_id)
    return row, store.descriptor(row)


async def connect(websocket: WebSocket, resource_id: str):
    """Relay one viewer to a resource worker.

    Each database step (ticket, authorization, checkpoint) runs to completion on
    a session of its own before the next network wait, so a long-lived viewer
    never pins a pooled connection.
    """
    origin = websocket.headers.get("origin", "")
    await websocket.accept()
    connection_id = uuid.uuid4().hex
    user_id = None
    row = None
    try:
        initial = await asyncio.wait_for(websocket.receive_json(), 5)
        attachment = await run_in_session(_consume_ticket, str(initial.get("ticket", "")), resource_id, origin)
        if attachment is None:
            await websocket.close(4401)
            return
        user_id, authentication = attachment

        async def authorize():
            await validate_authentication(authentication, user_id)
            return await run_in_session(service.authorized_row, resource_id, user_id)

        await validate_authentication(authentication, user_id)
        row, target = await run_in_session(_authorized_target, resource_id, user_id)
        send_lock = asyncio.Lock()
        async def send(data, binary=False):
            async with send_lock:
                if binary:
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_json(data)
        await send({"type": "attached", "connection_id": connection_id, "resource_id": resource_id})
        async def frames():
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=30), headers=target["headers"], trust_env=False) as client:
                async with client.stream("GET", target["url"] + "/events") as response:
                    response.raise_for_status()
                    async for data in packets(response.aiter_bytes()):
                        await send(data, True)
        async def receive():
            while True:
                payload = await websocket.receive_json()
                if len(json.dumps(payload)) > 24 * 1024 * 1024:
                    raise ValueError("command_too_large")
                current = await authorize()
                try:
                    if payload.get("action") == "checkpoint":
                        data = await service.checkpoint(current, str(payload.get("params", {}).get("name", "账号")), connection_id, transport=transport)
                    else:
                        data = await service.command(current, payload, actor="user", connection_id=connection_id, transport=transport)
                    await send({"type": "result", "id": payload.get("id"), "ok": True, "data": data})
                    if payload.get("action") == "close":
                        await service.close(await run_in_session(service.authorized_row, resource_id, user_id))
                        await websocket.close(4410)
                        return
                except HTTPException as exc:
                    await send({"type": "result", "id": payload.get("id"), "ok": False, "error": exc.detail})
        async def validate():
            while True:
                await asyncio.sleep(15)
                await authorize()
        async with RuntimeTransport(target) as transport:
            tasks = [asyncio.create_task(fn()) for fn in (frames, receive, validate)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except HTTPException as exc:
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(4410 if exc.status_code == 410 else 4403)
    except Exception as exc:
        logging.getLogger(__name__).warning("Resource stream %s failed: %s", resource_id, type(exc).__name__)
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(1011)
    finally:
        if row:
            try:
                await service.command(row, {"id": uuid.uuid4().hex, "action": "disconnect", "params": {}}, actor="user", connection_id=connection_id)
            except HTTPException:
                pass
