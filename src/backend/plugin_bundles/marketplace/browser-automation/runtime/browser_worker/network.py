"""Browser egress policy; intranet access must be explicitly configured."""
import ipaddress
from urllib.parse import urlsplit
from .resolver import BrowserResolver

class NetworkPolicy:
    def __init__(self, allowed_hosts=(), allow_private=False, dns_resolver_url=""):
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.resolver = BrowserResolver(dns_resolver_url)
        self.allow_private = allow_private

    async def check(self, url):
        parsed = urlsplit(url)
        if parsed.scheme == "about" and url == "about:blank":
            return
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError("unsupported_url")
        host = parsed.hostname.lower()
        return await self.addresses(host, parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80))

    async def addresses(self, host, port):
        addresses = await self.resolver.addresses(host, port, system=host in self.allowed_hosts)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0].split("%")[0])
            ip = getattr(ip, "ipv4_mapped", None) or ip
            if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                raise ValueError("blocked_destination")
            if not self.allow_private and host not in self.allowed_hosts and (not ip.is_global or getattr(ip, "is_site_local", False)):
                raise ValueError("private_destination_not_allowed")
        return addresses

    async def route(self, route):
        try:
            await self.check(route.request.url)
        except (ValueError, OSError):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def websocket(self, route):
        try:
            await self.check(route.url)
        except (ValueError, OSError):
            await route.close()
        else:
            route.connect_to_server()
