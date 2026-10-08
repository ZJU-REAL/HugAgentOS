"""Administrator-selected DNS transport; checked IPs remain pinned by egress."""
import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit
import httpx

class BrowserResolver:
    def __init__(self, endpoint=""):
        self.endpoint = endpoint
        self.cache = {}
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("invalid_dns_resolver_endpoint")

    async def addresses(self, host, port, *, system=False):
        try:
            ipaddress.ip_address(host)
            system = True
        except ValueError:
            pass
        if not self.endpoint or system:
            return await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        cached = self.cache.get(host)
        if cached and cached[0] > time.monotonic():
            values = cached[1]
        else:
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    answers = await asyncio.gather(*(self.query(client, host, kind) for kind in (1, 28)))
                values = [item for result in answers for item in result]
                if not values:
                    raise OSError("dns_name_unavailable")
                ttl = max(0, min(60, *(item[1] for item in values)))
                if len(self.cache) >= 128:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[host] = (time.monotonic() + ttl, values)
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                raise OSError("dns_resolution_failed") from exc
        return [(socket.AF_INET if ip.version == 4 else socket.AF_INET6,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (str(ip), port) if ip.version == 4 else (str(ip), port, 0, 0))
                for ip, _ in values]

    async def query(self, client, host, kind):
        body = bytearray()
        async with client.stream("GET", self.endpoint,
                params={"name": host.encode("idna").decode(), "type": kind, "edns_client_subnet": "0.0.0.0/0"},
                headers={"Accept": "application/dns-json"}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("dns_response_too_large")
        data = json.loads(body)
        if data.get("Status") == 3:
            return []
        if data.get("Status") != 0 or data.get("TC"):
            raise OSError("dns_resolution_failed")
        answers = []
        for item in data.get("Answer", []):
            if item.get("type") != kind:
                continue
            ip = ipaddress.ip_address(item["data"])
            if ip.version != (4 if kind == 1 else 6):
                raise ValueError("invalid_dns_record")
            answers.append((ip, int(item["TTL"])))
        return answers
