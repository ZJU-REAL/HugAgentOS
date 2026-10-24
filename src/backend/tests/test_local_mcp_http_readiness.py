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
