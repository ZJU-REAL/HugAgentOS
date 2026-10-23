"""FastAPI application for HugAgentOS.

This module is the slim orchestrator: it creates the FastAPI instance,
wires up middleware / error-handlers / routers, and defines lifecycle
events.  Heavy logic lives in dedicated sub-modules:

- api.middleware.cors          – CORS setup
- api.middleware.logging       – HTTP logging & request-size limit
- api.middleware.error_handler – global exception handlers
- api.health                   – /health, /ready, /live endpoints
"""

import os
import sys

from orchestration.configuration import register_configuration
register_configuration()

from features import register_run_capabilities

register_run_capabilities()
from features import register_model_credentials
register_model_credentials()
from integrations import register_credentials
register_credentials()

from api.lifecycle import lifespan
from api.openapi import _build_custom_openapi, _docs_title
from api.health import router as health_router
from api.middleware.cors import setup_cors
from api.middleware.edition import edition_router_dependencies, setup_edition_middleware
from api.middleware.error_handler import setup_error_handlers
from api.middleware.logging import setup_logging_middleware
from api.routes import files_router
from features.sites.api.sites_serve import router as sites_serve_router
from api.routes.v1 import (
    CE_ROUTERS,
    EE_ROUTERS,
    iter_edition_routers,
    login_router,
    mock_sso_router,
)
from core.config.settings import settings
from core.harness.registry import load_extensions
from core.harness.registry import routers as extension_routers
from core.infra.logging import get_logger
from features.installed import INSTALLED_FEATURES
from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

# Env files are loaded by core.config.settings (repo root only). The bare
# load_dotenv() that used to live here searched parent directories as well.
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lifespan (replaces the deprecated @app.on_event handlers). Startup steps run
# in the same order they were previously registered; the handler functions
# themselves are defined further below and resolved by late binding at startup.
# ---------------------------------------------------------------------------


# Startup is one declarative table. ``gate`` steps must finish before uvicorn
# opens the port (the health check promises a usable database and execution
# plane); everything else runs afterwards, in the same relative order, without
# holding the port. ``roles`` limits a step to the processes that own that
# responsibility: the hybrid-desktop local backend is only a local execution
# plane and never runs cloud-side workers.


# ---------------------------------------------------------------------------
# Create app
# ---------------------------------------------------------------------------

# The title/docs heading does not hard-code the brand: it defaults to the env
# brand (settings.branding.product_name), then at runtime the /docs and /redoc
# custom routes plus _custom_openapi override it live from the admin-platform config.
# docs_url/redoc_url are set to None to disable FastAPI's default docs routes,
# replaced by the custom routes below that inject the dynamic title.
app = FastAPI(
    title=f"{settings.branding.product_name} API",
    description="Multi-agent system with MCP integration",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Custom OpenAPI: inject the unified envelope into empty response schemas of all /v1/* endpoints
# ---------------------------------------------------------------------------
#
# By project convention every /v1/* endpoint returns
# { code, message, data, trace_id, timestamp } (see core/infra/responses.py).
# But most routes declare no response_model, so in the FastAPI auto-generated
# openapi.json those response schemas are empty `{}`.
#
# Here we post-process at OpenAPI generation time: when a /v1/* endpoint's 2xx
# response has no explicit schema, replace it with a reference to the
# `ApiResponseEnvelope` component. That way Swagger / ReDoc / the custom API
# docs page all display the envelope structure without touching any existing route.
#
# The `data` field stays as an arbitrary type (varies per endpoint); if a
# specific endpoint later needs to display concrete business fields, just
# declare a response_model on that route — this hook only injects when the
# schema is empty and never overrides an existing declaration.


app.openapi = _build_custom_openapi(app)  # type: ignore[assignment]


# Custom docs routes: title follows the admin-platform brand config (replacing FastAPI's default hard-coded app.title).
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=_docs_title(),
    )


@app.get("/redoc", include_in_schema=False)
async def custom_redoc_html():
    return get_redoc_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=_docs_title(),
    )


# ---------------------------------------------------------------------------
# Middleware & error handlers (order matters – last registered runs first)
# ---------------------------------------------------------------------------

# Registration order is the reverse of execution order (later registrations sit
# further out and run first). Edition middleware is innermost so CORS can still
# decorate any edition-specific rejection response.
setup_edition_middleware(app)
from api.middleware.agent_api_scope import AgentApiScopeMiddleware
app.add_middleware(AgentApiScopeMiddleware)
setup_cors(app)
setup_logging_middleware(app)
setup_error_handlers(app)

# Docker-free local mode: single process, single origin — nginx's `/api` prefix
# stripping is moved into the process (outermost, runs before routing). Only
# active when DEPLOY_PROFILE=local; zero impact on the compose path.
if settings.deploy.is_local:
    from api.local_hosting import setup_local_api_prefix

    setup_local_api_prefix(app)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


# Root endpoint
@app.get("/", tags=["root"], summary="服务根路径")
async def root():
    """API 根路径。

    常规部署返回服务信息 JSON；无 Docker 本地模式（单进程单源，无 nginx）则把 ``/``
    交给前端 SPA —— 直接返回 index.html，用户打开 http://127.0.0.1:<port>/ 即见应用。
    """
    if settings.deploy.is_local:
        from api.local_hosting import spa_index_response

        resp = spa_index_response()
        if resp is not None:
            return resp
    return {
        "service": app.title,
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
        "health": "/health",
    }


# Health / monitoring endpoints
app.include_router(health_router)

# Non-v1 routers (file downloads keep legacy path for artifact URL stability)
app.include_router(files_router)

# V1 API routers — community-capable routes first, then edition extensions.
# The CE registry contains no extension entries and the derived tree does not
# carry their modules. The full repository attaches edition policy dependencies
# while registering extension routers.
for _name, _router, _ in iter_edition_routers(CE_ROUTERS):
    app.include_router(_router)
for _name, _router, _feature in iter_edition_routers(EE_ROUTERS):
    app.include_router(
        _router,
        dependencies=edition_router_dependencies(_feature),
    )
# Optional modules mount their routers only when switched on.
from features import register_product_tools, register_package_sources, register_resource_owners

register_product_tools()
from features import register_catalog_options
register_catalog_options()
register_package_sources()
register_resource_owners()
load_extensions(INSTALLED_FEATURES)
for _router, _feature in extension_routers():
    app.include_router(_router, dependencies=edition_router_dependencies(_feature))

# Register static site catch-all after specific application data routes.
app.include_router(sites_serve_router)

# Unified login entry points: /login + /register (always on, coexisting with mock-sso)
app.include_router(login_router)

# Mock SSO pages (/mock-sso/*): registered in every mode except pure remote
# production — serving both as the primary login in mock mode and as a debug
# fallback entry in local mode.
_sso_login_mode = settings.sso.effective_login_mode
if _sso_login_mode in ("mock", "local"):
    app.include_router(mock_sso_router)
    logger.info("mock_sso_router_registered", login_mode=_sso_login_mode)

# Docker-free local mode: single process hosts the frontend static files + SPA
# fallback. Must be registered **after all API routes** (the catch-all
# `/{full_path:path}` only takes unmatched GETs). Zero impact on the compose path.
if settings.deploy.is_local:
    from api.local_hosting import mount_frontend_static

    mount_frontend_static(app)

# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    """Main entry point for running the server."""
    import uvicorn

    port = settings.server.port
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
