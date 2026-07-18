"""No-Docker local profile: single-origin frontend hosting + API prefix bridge.

In the compose deployment nginx serves the built frontend and reverse-proxies
``/api/*`` to the backend (stripping the ``/api`` prefix). The local/quick-install
profile has no nginx — one uvicorn process serves both. This module reproduces
those two nginx behaviours **only when ``DEPLOY_PROFILE=local``**, so the compose
path is completely untouched:

1. ``_LocalApiPrefixMiddleware`` — strips a leading ``/api`` from the request path
   so the frontend's default API base (``/api`` → ``/api/v1/*``, see
   ``src/frontend/src/api.ts``) reaches the backend routers mounted at ``/v1/*``.
   This is exactly the rewrite the Vite dev proxy / nginx did; doing it here means
   we don't have to fork the frontend build for local mode.
2. ``mount_frontend_static`` — serves the prebuilt ``src/frontend/dist`` with an
   SPA fallback: hashed assets are served as files, every other non-API GET
   returns ``index.html`` so the client-side router (``main.tsx`` dispatches
   ``/``, ``/admin``, ``/config``, ``/api-docs`` and the ``?share`` query) works
   on hard refresh.

Both are registered late (after all API routers) from ``api.app`` behind an
``if settings.deploy.is_local`` guard.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.infra.logging import get_logger

logger = get_logger(__name__)

# Backend path prefixes that must NEVER be served the SPA index.html — a miss
# here is a real 404, not a client-side route.
_API_PREFIXES = ("/v1", "/api", "/health", "/ready", "/live", "/docs", "/redoc",
                 "/openapi.json", "/login", "/register", "/mock-sso", "/files",
                 "/site")


class _LocalApiPrefixMiddleware:
    """Pure-ASGI middleware: rewrite ``/api`` → ``/`` (local single-origin bridge).

    ``/api/v1/foo`` → ``/v1/foo``. ``/api-docs`` (no trailing slash after ``api``)
    is a frontend SPA route and is left alone.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                scope = dict(scope)
                scope["path"] = path[4:] or "/"
                raw = scope.get("raw_path")
                if raw:
                    # raw_path is the undecoded path bytes (no query string).
                    stripped = raw[4:]
                    scope["raw_path"] = stripped or b"/"
        await self.app(scope, receive, send)


def _resolve_dist_dir() -> Optional[Path]:
    """Locate the built frontend ``dist/``.

    Priority: ``FRONTEND_DIST_DIR`` env (set by the CLI to the packaged assets)
    → repo-relative ``src/frontend/dist`` (dev / source checkout).
    """
    env_dir = os.getenv("FRONTEND_DIST_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        return p if (p / "index.html").exists() else None
    # src/backend/api/local_hosting.py → parents[2] = src/backend, .parent = src
    repo_src = Path(__file__).resolve().parents[2]
    candidate = repo_src / "frontend" / "dist"
    return candidate if (candidate / "index.html").exists() else None


def spa_index_response() -> Optional[FileResponse]:
    """Return the SPA ``index.html`` as a response, or ``None`` if not built.

    Used by the ``/`` route in local mode so the root path serves the app instead
    of the API info JSON.
    """
    dist = _resolve_dist_dir()
    if dist is None:
        return None
    return FileResponse(str(dist / "index.html"))


def setup_local_api_prefix(app: FastAPI) -> None:
    """Register the ``/api`` prefix-strip middleware (outermost)."""
    app.add_middleware(_LocalApiPrefixMiddleware)
    logger.info("local_api_prefix_bridge_enabled")


def mount_frontend_static(app: FastAPI) -> None:
    """Mount the prebuilt frontend with SPA fallback (idempotent, best-effort).

    No-op with a warning if ``dist/`` isn't present, so the backend still boots
    (API-only) — the CLI's ``doctor`` surfaces the missing build separately.
    """
    dist = _resolve_dist_dir()
    if dist is None:
        logger.warning(
            "frontend_dist_missing",
            hint="build src/frontend (npm run build) or set FRONTEND_DIST_DIR",
        )
        return

    # Hashed build assets (immutable) served directly.
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    index_html = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        # Never mask a real backend route as a client-side one.
        req_path = "/" + full_path
        if any(req_path == p or req_path.startswith(p + "/") for p in _API_PREFIXES):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Serve a concrete static file (favicon, manifest, etc.) if it exists…
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist)  # containment guard against ../ traversal
        except ValueError:
            candidate = index_html
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        # …otherwise hand back index.html for the SPA router.
        return FileResponse(str(index_html))

    logger.info("frontend_static_mounted", dist=str(dist))
