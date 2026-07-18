"""Global license deactivation gate (the third line of defense for the commercial edition's "stop on expiry").

First line: the route registry (the CE tree physically contains no EE routes).
Second line: the ``requires_feature`` capability-bit guard (disables individual EE capabilities by entitlement).
This middleware is a **whole-product-level** gate: when the license is in :data:`DEAD_MODES`
(expired / invalid / missing), all requests except the allowlist are rejected with 402 —
even basic chat/data-fetching stops, delivering "the product stops the moment the license expires".

Allowlist (still reachable in the deactivated state, otherwise self-service renewal would be impossible):
- health/liveness probes, root path, API docs
- ``/v1/config/license``: upload a new license to renew (CONFIG_TOKEN auth)
- ``/v1/meta``: the frontend renders the "deactivated" block page from this

Decision goes through ``license_manager.is_active()``, and the result is cached by the license file's mtime,
so each request costs only one in-memory comparison and introduces no IO. ce / internal / licensed / grace
are always allowed — the community edition and internal/fully-hosted deployments are unaffected.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from core.infra.responses import generate_trace_id
from core.licensing import license_manager

# Path prefixes still allowed in the deactivated state (exact or prefix match).
_ALLOW_PREFIXES = (
    "/health",
    "/ready",
    "/live",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/v1/config/license",
    "/v1/meta",
    # Desktop client auto-update manifest / installer distribution: public, no user data, allowed
    # even when the license is invalid, so customers can pull a fixed client (otherwise
    # "expired -> update -> still expired" deadlocks).
    "/v1/desktop",
)

_INACTIVE_CODE = 40203
_INACTIVE_MESSAGE = "license 已失效或过期，产品已停用，请联系厂商续期后重新激活。"


def _allowed(path: str) -> bool:
    if path == "/":
        return True
    return any(path == p or path.startswith(p + "/") for p in _ALLOW_PREFIXES)


class LicenseGateMiddleware:
    """Pure ASGI middleware — gates business requests in the deactivated state, without wrapping the response body (does not break SSE)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")

        # CORS preflight and allowlist paths are always allowed; when the license is healthy, pass through directly.
        if method == "OPTIONS" or _allowed(path) or license_manager.is_active():
            await self.app(scope, receive, send)
            return

        body = json.dumps({
            "code": _INACTIVE_CODE,
            "message": _INACTIVE_MESSAGE,
            "data": {"mode": license_manager.mode()},
            "trace_id": generate_trace_id(),
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 402,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def setup_license_gate(app: FastAPI) -> None:
    app.add_middleware(LicenseGateMiddleware)
