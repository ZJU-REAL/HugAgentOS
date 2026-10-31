"""Pooled, patient HTTP for the local plane's synchronous calls to the cloud.

Every manifest poll, bundle download and work-copy sync used to go out through a
one-off ``httpx.get``: a fresh connection pool per call, so each call paid DNS,
TCP and a full TLS handshake, with five seconds for all of it. Sign-in fires
several of these at once, and on a laptop behind a VPN or TUN proxy a cold
handshake regularly runs past five seconds. The raw ``_ssl.c:999: The handshake
operation timed out`` then sat in the sync status, and the UI showed it the
moment a page opened, long after the background attempt had failed.

One shared client keeps connections warm between calls, the connect budget is
configurable, and a connect timeout gets exactly one more try: nothing has been
sent at that point, so repeating is safe for any method. Transport failures
surface as ``CloudUnavailable`` with a message written for the person; the raw
exception goes to the log.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

import httpx

from core.harness.capability_errors import CapabilityError, CloudUnavailable

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_ENV = "HUGAGENT_CLOUD_CONNECT_TIMEOUT_S"
DEFAULT_CONNECT_TIMEOUT_S = 15.0

# Retire pooled sockets before the gateway nginx's 65 s keep-alive closes them,
# so a reused connection is never one the server already dropped.
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8, keepalive_expiry=55.0)

_lock = threading.Lock()
_client: Optional[httpx.Client] = None


def connect_timeout_s() -> float:
    """TCP connect plus TLS handshake budget; bad or non-positive values fall back."""
    raw = os.getenv(CONNECT_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_CONNECT_TIMEOUT_S
    except ValueError:
        value = DEFAULT_CONNECT_TIMEOUT_S
    return value if value > 0 else DEFAULT_CONNECT_TIMEOUT_S


def _shared() -> httpx.Client:
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(limits=_LIMITS)
        return _client


def request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json: Any = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """Send one cloud request; ``timeout`` bounds reads, the connect budget is shared."""
    budget = httpx.Timeout(timeout, connect=connect_timeout_s())
    try:
        try:
            return _shared().request(method, url, headers=headers, json=json, timeout=budget)
        except httpx.ConnectTimeout as first:
            logger.info("[cloud-http] %s %s connect timed out, retrying once: %s", method, url, first)
            return _shared().request(method, url, headers=headers, json=json, timeout=budget)
    except httpx.TransportError as exc:
        logger.warning("[cloud-http] %s %s failed: %r", method, url, exc)
        raise CloudUnavailable(_transport_message(exc)) from exc


def get(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> httpx.Response:
    return request("GET", url, headers=headers, timeout=timeout)


def _transport_message(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "连接云端超时，请检查网络或代理设置后重试"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接云端，请检查网络或代理设置后重试"
    return "与云端的连接中断，请稍后重试"


def describe(exc: BaseException) -> str:
    """The sync error a person sees: no URLs, no ``_ssl.c`` internals."""
    if isinstance(exc, CapabilityError):
        return exc.message
    if isinstance(exc, httpx.HTTPStatusError):
        return f"云端暂时不可用（HTTP {exc.response.status_code}），请稍后重试"
    if isinstance(exc, httpx.TransportError):
        return _transport_message(exc)
    return str(exc)
