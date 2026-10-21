"""SOCKS5 transport validates and connects to the same resolved IP address."""
import asyncio
import ipaddress
import struct

class BrowserEgress:
    def __init__(self, policy):
        self.policy = policy
        self.clients = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        return "socks5://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        remote = None
        try:
            async with asyncio.timeout(15):
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                if version != 5 or 0 not in methods:
                    return
                writer.write(b"\x05\x00")
                await writer.drain()
                version, command, reserved, kind = await reader.readexactly(4)
                if version != 5 or command != 1 or reserved:
                    return
                if kind == 3:
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode("idna")
                elif kind in (1, 4):
                    host = str(ipaddress.ip_address(await reader.readexactly(4 if kind == 1 else 16)))
                else:
                    return
                port = struct.unpack(">H", await reader.readexactly(2))[0]
                addresses = await self.policy.addresses(host, port)
                last_error = None
                for family, _, _, _, address in addresses:
                    try:
                        # A literal checked address prevents a second DNS lookup.
                        incoming, remote = await asyncio.open_connection(address[0], port, family=family)
                        break
                    except OSError as exc:
                        last_error = exc
                else:
                    raise last_error or OSError("destination_unavailable")
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
            async def copy(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            tasks = [asyncio.create_task(copy(reader, remote)), asyncio.create_task(copy(incoming, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for child in tasks:
                    child.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for stream in (remote, writer):
                if stream:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except (OSError, asyncio.CancelledError):
                        pass
            self.clients.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.clients):
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
