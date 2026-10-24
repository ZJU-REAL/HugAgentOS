"""Keep a slow desktop resolver off the critical path of every message.

The local execution plane opens a fresh connection to the cloud for practically
every message: the gateway's nginx closes idle keep-alive connections after 65
seconds and a person takes longer than that to type the next question, so the
pool is empty by the time they hit send. That is normally fine — connecting and
negotiating TLS to the gateway costs about 40 ms — but resolving the host is not
always cheap on a user's own machine. On a laptop running a VPN resolver
alongside several interface resolvers, one lookup was measured at 11.0 seconds,
consistently, and the local plane paid it once per message: more than the model,
the payload and the whole agent assembly combined.

The operating system is meant to cache this. Where it does, this cache returns
its first answer and costs nothing. Where it does not, this is what stops a
misbehaving resolver from being charged to the user on every turn. Only
successful answers are cached, and only for ``ttl_s``, so a proxy restart or a
network change is picked up at the next expiry instead of persisting for the
life of the process.

Deliberately not installed on a cloud deployment: there the resolver is a
datacenter cache answering in microseconds, and masking a resolution change
would be a liability rather than a saving. ``core.config.local_mode`` owns that
decision.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

DEFAULT_TTL_S = 300.0
MAX_ENTRIES = 256


_ttl_s = DEFAULT_TTL_S


def _store(key: Tuple[Any, ...], answer: Any, now: float) -> None:
    with process_state().lock:
        if len(process_state().cache) >= MAX_ENTRIES:
            for stale in [k for k, (expires, _) in process_state().cache.items() if expires <= now]:
                del process_state().cache[stale]
            if len(process_state().cache) >= MAX_ENTRIES:
                process_state().cache.clear()
        process_state().cache[key] = (now + _ttl_s, answer)


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    key = (host, port, family, type, proto, flags)
    now = time.monotonic()
    with process_state().lock:
        hit = process_state().cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    # A failure is never cached: the exception propagates from here, so a name
    # that starts resolving is picked up on the very next attempt.
    answer = process_state().original(host, port, family, type, proto, flags)
    _store(key, answer, now)
    return answer


def install(ttl_s: Optional[float] = None) -> bool:
    """Install the cache process-wide. Idempotent; True the first time it takes."""
    global _ttl_s

    configured = os.getenv("HUGAGENT_DNS_CACHE_TTL_S")
    _ttl_s = float(configured) if configured else float(DEFAULT_TTL_S if ttl_s is None else ttl_s)
    if _ttl_s <= 0 or process_state().original is not None:
        return False
    process_state().original = socket.getaddrinfo
    socket.getaddrinfo = _cached_getaddrinfo
    return True


def uninstall() -> None:

    clear()
    if process_state().original is not None:
        socket.getaddrinfo = process_state().original
        process_state().original = None


def clear() -> None:
    with process_state().lock:
        process_state().cache.clear()


def installed() -> bool:
    return process_state().original is not None


@dataclass
class DnsCacheState:
    lock: object = field(default_factory=lambda: threading.Lock())
    cache: Dict[Tuple[Any, ...], Tuple[float, Any]] = field(default_factory=lambda: {})
    original: object = None


_STATE = DnsCacheState()


def process_state() -> DnsCacheState:
    return _STATE
