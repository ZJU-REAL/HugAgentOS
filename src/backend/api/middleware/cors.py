"""CORS middleware configuration."""

import logging
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config.settings import settings

logger = logging.getLogger(__name__)

# Site dynamic API path (/site/<slug>/__api/**)
_SITE_API_RE = re.compile(r"^/site/[^/]+/__api/")

_SITE_API_CORS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"),
    (b"access-control-allow-headers", b"Content-Type"),
    (b"access-control-max-age", b"600"),
]


class SiteApiCorsMiddleware:
    """CORS pre-layer for the site API (must be added after the global CORSMiddleware = executes first).

    Public sites run with ``CSP: sandbox`` under an opaque origin (``Origin: null``);
    in-site fetch preflights hit the global CORSMiddleware first — null is not in (and
    should not be added to) the credentials whitelist, so it gets blocked with 400. Here
    we answer OPTIONS on ``/site/<slug>/__api/**`` directly with 204 + ``ACAO: *``
    (credential-less semantics, since the site API is meant for anonymous visitors), and
    pass every other request through unchanged (the actual response CORS headers are
    filled in by the route layer).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "OPTIONS"
            and _SITE_API_RE.match(scope["path"])
        ):
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": list(_SITE_API_CORS),
            })
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def setup_cors(app: FastAPI) -> None:
    """Configure CORS middleware based on environment.

    In production, uses the domain whitelist from CORS_ORIGINS env var.
    In development, allows all origins.
    """
    allowed_origins_str = settings.server.cors_origins
    configured_origins = [
        origin.strip()
        for origin in allowed_origins_str.split(",")
        if origin.strip()
    ]

    if settings.server.is_prod:
        allowed_origins = configured_origins
        if not allowed_origins:
            logger.warning(
                "CORS_ORIGINS is empty in production mode. "
                "No cross-origin requests will be allowed. "
                "Set CORS_ORIGINS env var to a comma-separated list of allowed origins."
            )
    else:
        if configured_origins and configured_origins != ["*"]:
            allowed_origins = configured_origins
        else:
            allowed_origins = [
                "http://localhost:3000",
                "http://localhost:3002",
                "http://localhost:3005",
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:3002",
                "http://127.0.0.1:3005",
                "http://127.0.0.1:5173",
            ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last = outermost executes first: site API preflight bypasses the global credentials whitelist logic
    app.add_middleware(SiteApiCorsMiddleware)
