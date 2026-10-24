"""Live desktop stream: a token-checked WebSocket in front of the private VNC socket.

Runs as the desktop user. The task user can reach this port but cannot read the token,
and the VNC socket itself sits in a directory only the desktop user can open.
"""
import asyncio
import hmac

from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

TOKEN_FILE = "/run/pa-desktop/stream.token"
VNC_SOCKET = "/run/pa-desktop/vnc.sock"
# The backend dials this port (personal_agent/computer/sandbox.py STREAM_PORT).
PORT = 6090


def token():
    try:
        with open(TOKEN_FILE) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def check(connection, request):
    expected = token()
    given = request.headers.get("X-PA-Stream-Token", "")
    if not expected or not hmac.compare_digest(given, expected):
        return Response(403, "Forbidden", Headers(), b"Forbidden\n")
    return None


async def relay(websocket):
    reader, writer = await asyncio.open_unix_connection(VNC_SOCKET)

    async def upstream():
        async for message in websocket:
            writer.write(message if isinstance(message, bytes) else message.encode())
            await writer.drain()

    async def downstream():
        while data := await reader.read(256 * 1024):
            await websocket.send(data)

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks: task.cancel()
        writer.close()


async def main():
    # No compression: the hop is local and VNC data is already encoded.
    async with serve(relay, "0.0.0.0", PORT, process_request=check, compression=None, max_size=4 * 1024 * 1024,
                     ping_interval=20, ping_timeout=20):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
