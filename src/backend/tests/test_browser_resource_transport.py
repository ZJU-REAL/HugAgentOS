"""Commands reuse a private runtime connection without replaying failures."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from core.plugins.resources import service
from core.plugins.resources.transport import RuntimeTransport


async def test_commands_reuse_one_tcp_connection_and_keep_runtime_authentication():
    connections, requests = [], []
    handlers = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connections.append(writer)
        try:
            while True:
                try:
                    raw = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    return
                lines = raw.decode().split("\r\n")
                headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
                body = await reader.readexactly(int(headers.get("Content-Length", 0)))
                requests.append((headers, json.loads(body)))
                data = b'{"ok":true}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(data)).encode() + b"\r\n\r\n" + data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    target = {"url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
              "headers": {"X-Hugagent-Resource-Token": "fixture-only"}}
    try:
        async with RuntimeTransport(target) as transport:
            for index in range(3):
                assert await service.command(SimpleNamespace(), {"id": str(index), "action": "input"},
                    actor="user", connection_id="viewer", transport=transport) == {"ok": True}
            with pytest.raises(HTTPException) as denied:
                await service.command(SimpleNamespace(), {"action": "checkpoint"},
                    actor="user", transport=transport)
            assert denied.value.status_code == 403
        assert len(connections) == 1
        assert len(requests) == 3
        assert all(headers["X-Hugagent-Resource-Token"] == "fixture-only" for headers, _ in requests)
        assert all(body["actor"] == "user" and body["connection_id"] == "viewer" for _, body in requests)
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(handlers))


async def test_websocket_revocation_stops_commands_and_closes_transport(monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from core.plugins.resources import stream, authentication
    from contextlib import nullcontext

    bodies, writers, handlers, transports = [], [], set(), []
    closing = asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        writers.append(writer)
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                if raw.startswith(b"GET /events "):
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                                 b"Connection: close\r\n\r\n")
                    await writer.drain()
                    await closing.wait()
                    return
                headers = dict(line.split(": ", 1) for line in raw.decode().split("\r\n")[1:]
                               if ": " in line)
                bodies.append(json.loads(await reader.readexactly(int(headers["Content-Length"]))))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    target = {"url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
              "headers": {"X-Hugagent-Resource-Token": "fixture-only"}}

    class TrackedTransport(RuntimeTransport):
        def __init__(self, target):
            super().__init__(target)
            transports.append(self)

    class Socket:
        headers = {"origin": "http://fixture"}
        client_state = SimpleNamespace(name="CONNECTED")
        count = 0
        close_code = None

        async def accept(self):
            pass

        async def receive_json(self):
            self.count += 1
            if self.count == 1:
                return {"ticket": "fixture-ticket"}
            if self.count <= 4:
                return {"id": str(self.count), "action": "input", "params": {}}
            raise WebSocketDisconnect()

        async def send_json(self, payload):
            pass

        async def send_bytes(self, payload):
            pass

        async def close(self, code):
            self.close_code = code
            self.client_state = SimpleNamespace(name="DISCONNECTED")

    validations = []
    async def validate(*args):
        validations.append(True)
        if len(validations) == 4:
            raise HTTPException(403, "fixture_revoked")

    monkeypatch.setattr(stream, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(stream, "RuntimeTransport", TrackedTransport)
    monkeypatch.setattr(stream.store, "consume", lambda *args, **kwargs: ("fixture-user", {}))
    monkeypatch.setattr(stream.store, "descriptor", lambda row: target)
    monkeypatch.setattr(service, "authorized", lambda *args: SimpleNamespace())
    monkeypatch.setattr(authentication, "validate", validate)
    socket = Socket()
    try:
        await asyncio.wait_for(stream.connect(socket, "fixture-resource"), 5)
        assert socket.close_code == 4403
        assert len(validations) == 4
        assert [body["action"] for body in bodies] == ["input", "input", "disconnect"]
        assert len(transports) == 1 and transports[0].client.is_closed
    finally:
        closing.set()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        await asyncio.gather(*tuple(handlers))
