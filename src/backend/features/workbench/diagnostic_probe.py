"""Public HTTPS HEAD probe; DNS checked at the actual socket connection.

No proxy environment, redirect following, cookies, credentials or response body.
The socket is connected to a validated numeric address, preserving TLS SNI.
"""

import http.client
import ipaddress
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from features.workbench.external import validate_url
from fastapi import HTTPException

_slots = BoundedSemaphore(2)
_resolver = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workbench-probe-dns")


def denied():
    raise HTTPException(422, detail={"code": "probe_target_denied"})


def public_addresses(host, port):
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, detail={"code": "probe_busy"})
    future = _resolver.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    future.add_done_callback(lambda _: _slots.release())
    try:
        entries = future.result(timeout=2)
    except (TimeoutError, OSError):
        future.cancel()
        raise HTTPException(502, detail={"code": "probe_failed"}) from None
    if not entries:
        denied()
    for entry in entries:
        address = ipaddress.ip_address(entry[4][0])
        if (
            not address.is_global
            or address.is_multicast
            or getattr(address, "ipv4_mapped", None) is not None
        ):
            denied()
    return entries


class CheckedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        entries = public_addresses(self.host, self.port)
        last = None
        for family, socktype, proto, _, address in entries[:4]:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                # Recheck the actual peer rather than doing a second DNS lookup.
                if not ipaddress.ip_address(sock.getpeername()[0]).is_global:
                    sock.close()
                    denied()
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as exc:
                sock.close()
                last = exc
        raise OSError("probe connection failed") from last


def probe(url):
    validate_url(url)
    parts = urlsplit(url)
    # No arbitrary ports, query strings, plaintext HTTP, or redirect traversal.
    if parts.scheme != "https" or (parts.port or 443) != 443 or parts.query or parts.fragment:
        denied()
    connection = CheckedHTTPSConnection(
        parts.hostname, port=443, timeout=2, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "HEAD",
            parts.path or "/",
            headers={"Accept": "*/*", "User-Agent": "HugAgentOS-Diagnostic/1"},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            denied()
        return {"status": response.status, "reachable": response.status < 500}
    except HTTPException:
        raise
    except (OSError, http.client.HTTPException):
        raise HTTPException(502, detail={"code": "probe_failed"}) from None
    finally:
        connection.close()
