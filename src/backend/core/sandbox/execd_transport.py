"""Keep execd endpoint metadata and SDK HTTP transports on the same route.

OpenSandbox 0.1.x builds generated REST clients eagerly, while command SSE URLs
read execd_endpoint lazily. Updating only that endpoint leaves status/logs on the
controller proxy. Keep the existing clients so headers, retries, pools and
transport ownership remain unchanged. Call only before publishing a new handle.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_SERVICES = (
    "_command_service",
    "_filesystem_service",
    "_health_service",
    "_metrics_service",
    "_isolated_service",
)


def repoint_execd_clients(sandbox: Any, endpoint: str) -> bool:
    """Switch all supported execd clients together, or leave the proxy intact."""
    changes: list[tuple[Any, str, Any, Any]] = []
    seen: set[tuple[int, str]] = set()

    def add(obj: Any, name: str, value: Any) -> None:
        key = (id(obj), name)
        if key not in seen:
            seen.add(key)
            changes.append((obj, name, getattr(obj, name), value))

    try:
        for name in _SERVICES:
            service = getattr(sandbox, name, None)
            if service is None:
                continue
            ep = getattr(service, "execd_endpoint", None)
            if ep is None:
                return False
            config = service.connection_config
            # Execd listens on plain HTTP. A TLS controller configuration must
            # keep its proxy until the SDK supports a separate execd scheme.
            if config.protocol != "http":
                return False
            client = service._httpx_client
            generated = getattr(service, "_client", None)
            if not isinstance(client, httpx.AsyncClient):
                return False
            if generated is not None and generated.get_async_httpx_client() is not client:
                return False
            url = f"http://{endpoint}"
            add(ep, "endpoint", endpoint)
            add(client, "base_url", httpx.URL(url + "/"))
            # Retain the generated client's source URL as well (with_headers /
            # with_timeout may later clone it and create a new HTTP client).
            if generated is not None:
                add(generated, "_base_url", url)
            elif name != "_isolated_service":
                return False
    except AttributeError:
        logger.warning("[opensandbox] unsupported SDK execd client layout; keeping proxy")
        return False
    if not changes:
        return False
    applied = []
    try:
        for obj, name, old, new in changes:
            setattr(obj, name, new)
            applied.append((obj, name, old))
    except Exception:
        for obj, name, old in reversed(applied):
            setattr(obj, name, old)
        logger.warning("[opensandbox] execd route update failed; restored proxy", exc_info=True)
        return False
    return True
