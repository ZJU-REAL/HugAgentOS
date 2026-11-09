"""MCP readiness waits for the listener to accept and answer the connection."""
import asyncio

import pytest

from core.infra.local_runtime.startup import _http_port_ready


@pytest.mark.asyncio
async def test_http_probe_waits_for_response_even_when_root_route_is_missing():
    requests = []
    async def serve(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        await asyncio.sleep(0.05)
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        assert await _http_port_ready("127.0.0.1", server.sockets[0].getsockname()[1])
    assert requests[0].startswith(b"HEAD / HTTP/1.1\r\n")


@pytest.mark.asyncio
async def test_tcp_accept_without_an_http_response_is_not_ready():
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        assert not await _http_port_ready("127.0.0.1", server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_script_runner_start_waits_for_http_response(monkeypatch):
    from types import SimpleNamespace
    from core.infra.local_runtime import lifecycle, startup

    requests = []
    server = None
    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    # Reserve a free port, then let the actual spawn boundary start the listener.
    reservation = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = reservation.sockets[0].getsockname()[1]
    reservation.close()
    await reservation.wait_closed()

    async def spawn(label, argv):
        nonlocal server
        server = await asyncio.start_server(serve, "127.0.0.1", port)
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(startup, "settings", SimpleNamespace(
        sandbox=SimpleNamespace(provider="script_runner", runner_url=f"http://127.0.0.1:{port}")
    ))
    monkeypatch.setattr(startup, "_spawn", spawn)
    try:
        await asyncio.wait_for(lifecycle._start_script_runner("python"), timeout=3)
        assert requests and requests[0].startswith(b"HEAD / HTTP/1.1")
    finally:
        if server:
            server.close()
            await server.wait_closed()
