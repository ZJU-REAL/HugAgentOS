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
from contextlib import asynccontextmanager
from typing import Callable

from api.health import router as health_router
from api.middleware.cors import setup_cors
from api.middleware.edition import edition_router_dependencies, setup_edition_middleware
from api.middleware.error_handler import setup_error_handlers
from api.middleware.logging import setup_logging_middleware
from api.routes import files_router, sites_serve_router
from api.routes.v1 import (
    CE_ROUTERS,
    EE_ROUTERS,
    iter_edition_routers,
    login_router,
    mock_sso_router,
)
from core.config.settings import settings
from core.infra.logging import get_logger
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lifespan (replaces the deprecated @app.on_event handlers). Startup steps run
# in the same order they were previously registered; the handler functions
# themselves are defined further below and resolved by late binding at startup.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    await _startup_ensure_tables()
    await _startup_seed_ce_admin()
    await _startup_local_sidecars()
    await _startup_recover_chat_runs()
    await _startup_resume_loops()
    await _startup_stale_run_reaper()
    await _startup_warm_sandbox_pool()
    await _startup_idle_session_reaper()
    await _startup_seed_page_config()
    await _startup_seed_prompt_versions()
    await _startup_seed_roles()
    await _startup_seed_mcp_servers()
    await _startup_seed_default_plugins()
    await _startup_recover_datasource_sidecars()
    await _startup_seed_ontologies()
    await _startup_preload()
    await _startup_automation_scheduler()
    await _startup_distillation_scheduler()
    await _startup_recover_persona_distill_jobs()
    await _startup_warmup_memory()
    await _startup_channel_manager()
    yield
    # ── shutdown ──
    await _shutdown_stale_run_reaper()
    await _shutdown_channel_manager()
    await _shutdown_datasource_sidecar_recovery()
    await _shutdown_local_sidecars()
    await _shutdown_pools()


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


def _build_custom_openapi():
    from api.openapi_data_schemas import DATA_COMPONENTS, DATA_SCHEMAS
    from api.openapi_edition_schemas import EDITION_DATA_COMPONENTS, EDITION_DATA_SCHEMAS
    from fastapi.openapi.utils import get_openapi

    def _custom_openapi():
        if app.openapi_schema:
            # The heavy schema is cached, but the brand title is refreshed from the admin-platform config every time (renames take effect immediately)
            app.openapi_schema["info"]["title"] = _docs_title()
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        components = schema.setdefault("components", {}).setdefault("schemas", {})

        # Inject business-entity component schemas (from the central registry)
        for name, comp_schema in {**DATA_COMPONENTS, **EDITION_DATA_COMPONENTS}.items():
            if name not in components:
                components[name] = comp_schema

        if "ApiResponseEnvelope" not in components:
            components["ApiResponseEnvelope"] = {
                "title": "ApiResponseEnvelope",
                "type": "object",
                "description": (
                    "统一 API 响应包络。所有 /v1/* 接口都返回此结构；"
                    "实际业务数据放在 `data` 字段中（不同端点结构不同）。"
                ),
                "required": ["code", "message", "trace_id", "timestamp"],
                "properties": {
                    "code": {
                        "type": "integer",
                        "default": 10000,
                        "description": "业务状态码（10000 = 成功；非 10000 表示业务错误）",
                    },
                    "message": {
                        "type": "string",
                        "default": "Success",
                        "description": "人类可读消息（成功/失败描述）",
                    },
                    "data": {
                        "description": (
                            "端点特定的业务数据。不同接口结构不同——"
                            "通用结构是 object/array/null，详细字段见各接口的"
                            "「响应」描述或后端服务层定义。"
                        ),
                    },
                    "trace_id": {
                        "type": "string",
                        "example": "req_a1b2c3d4e5f60718",
                        "description": "请求追踪 ID（用于日志关联），格式 `req_<16hex>`",
                    },
                    "timestamp": {
                        "type": "integer",
                        "format": "int64",
                        "example": 1735660800000,
                        "description": "响应生成时间，Unix 毫秒时间戳",
                    },
                },
            }

        envelope_ref = {"$ref": "#/components/schemas/ApiResponseEnvelope"}

        def _is_empty_schema(s):
            if not isinstance(s, dict):
                return True
            if not s:
                return True
            informative_keys = {
                "$ref",
                "type",
                "properties",
                "items",
                "allOf",
                "anyOf",
                "oneOf",
                "enum",
                "additionalProperties",
                "format",
            }
            return not any(k in s for k in informative_keys)

        def _envelope_schema_for(method_upper: str, path: str) -> dict:
            """Registry hit → allOf merge; otherwise fall back to a plain envelope ref."""
            data_schema = EDITION_DATA_SCHEMAS.get(
                (method_upper, path), DATA_SCHEMAS.get((method_upper, path))
            )
            if data_schema is None:
                return dict(envelope_ref)
            return {
                "allOf": [
                    dict(envelope_ref),
                    {
                        "type": "object",
                        "properties": {"data": data_schema},
                    },
                ],
            }

        for path, methods in (schema.get("paths") or {}).items():
            if not path.startswith("/v1/"):
                continue
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not isinstance(op, dict):
                    continue
                method_upper = method.upper()
                responses = op.get("responses") or {}
                for status, resp in responses.items():
                    if not (isinstance(status, str) and status.startswith("2")):
                        continue
                    if not isinstance(resp, dict):
                        continue
                    content = resp.get("content")
                    if not isinstance(content, dict):
                        # When no content is declared, add one too so the frontend can render the envelope
                        resp["content"] = {
                            "application/json": {"schema": _envelope_schema_for(method_upper, path)}
                        }
                        continue
                    for media, payload in content.items():
                        if not isinstance(media, str) or not media.startswith("application/json"):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if _is_empty_schema(payload.get("schema")):
                            payload["schema"] = _envelope_schema_for(method_upper, path)

        schema["info"]["title"] = _docs_title()
        app.openapi_schema = schema
        return schema

    return _custom_openapi


def _docs_title() -> str:
    """Title for the docs pages (Swagger / ReDoc) and OpenAPI info.title — fetched
    live from the admin-platform config (product_name + API-docs label),
    removing the hard-coded brand. Falls back to app.title when the DB read
    fails (already the env brand, no literal "HugAgentOS")."""
    try:
        from core.content.content_blocks import get_admin_platform_info

        info = get_admin_platform_info()
        name = (info.get("product_name") or "").strip()
        label = (info.get("apidoc_label") or "").strip()
        joined = " ".join(p for p in (name, label) if p)
        return joined or app.title
    except Exception:
        return app.title


app.openapi = _build_custom_openapi()  # type: ignore[assignment]


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

# Public site hosting (/site/{slug}/…): nginx location /site/ reverse-proxies here verbatim
app.include_router(sites_serve_router)

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


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _startup_preload_enabled() -> bool:
    return _env_flag("STARTUP_PRELOAD_ENABLED", True)


def _mcp_warmup_enabled() -> bool:
    return _env_flag("MCP_WARMUP_ENABLED", True)


async def _startup_ensure_tables():
    """Ensure database tables exist for SQLite environments.

    Alembic migration files use PostgreSQL-specific DDL that cannot run on
    SQLite.  When the entrypoint script is bypassed (e.g. direct ``uvicorn``
    invocation during local development), this handler guarantees that all
    ORM tables are created via ``Base.metadata.create_all()`` which respects
    the dialect-aware type variants.  For PostgreSQL this is a no-op because
    tables are already managed by alembic.
    """
    from core.db.engine import DATABASE_URL, init_db

    if DATABASE_URL.startswith("sqlite://"):
        logger.info("[startup] SQLite detected – ensuring tables via create_all()")
        init_db()


async def _startup_seed_ce_admin():
    """Seed the CE bootstrap administrator without affecting EE deployments."""
    if settings.edition.edition != "ce":
        return
    try:
        from core.db.engine import SessionLocal
        from core.services.local_user_service import ensure_ce_default_admin

        db = SessionLocal()
        try:
            user_id, created = ensure_ce_default_admin(db)
            if created:
                logger.info("[startup] CE default administrator created: %s", user_id)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] CE default administrator seed failed: %s", exc)


async def _startup_recover_chat_runs():
    """Mark running/pending chat_runs left over from before the last process crash as failed."""
    try:
        from orchestration import chat_run_executor

        count = await chat_run_executor.recover_orphan_runs()
        if count:
            logger.info("[startup] chat_runs orphan recovered: %d", count)
    except Exception as exc:
        logger.warning("[startup] chat_runs orphan recovery failed: %s", exc)


async def _startup_resume_loops():
    """Resume interrupted autonomous loops (M4, off by default; enable with LOOP_AUTO_RESUME=true)."""
    try:
        from orchestration import chat_run_executor

        count = await chat_run_executor.resume_running_loops()
        if count:
            logger.info("[startup] autonomous loops resumed: %d", count)
    except Exception as exc:
        logger.warning("[startup] autonomous loop resume failed: %s", exc)


async def _startup_stale_run_reaper():
    """Periodically reap zombie chat_runs stuck in running (fallback behind the watchdog)."""
    try:
        import asyncio

        from orchestration import chat_run_executor

        task = asyncio.create_task(chat_run_executor.run_stale_reaper_loop())
        app.state.stale_run_reaper_task = task
        logger.info("[startup] chat_run stale reaper started")
    except Exception as exc:
        logger.warning("[startup] chat_run stale reaper start failed: %s", exc)


async def _shutdown_stale_run_reaper():
    import asyncio
    import contextlib

    task = getattr(app.state, "stale_run_reaper_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _shutdown_channel_manager():
    try:
        from core.channels.manager import get_manager

        get_manager().shutdown()
    except Exception:  # noqa: BLE001
        pass


async def _startup_warm_sandbox_pool():
    """Provider-agnostic warmup hook: call provider.warmup() so providers that
    support prewarming (opensandbox) start background pool filling. Providers
    that don't have this method → no-op.
    """
    try:
        from core.sandbox import get_sandbox_provider

        provider = get_sandbox_provider()
        warmup = getattr(provider, "warmup", None)
        if warmup is None:
            return
        await warmup()
        logger.info("[startup] sandbox provider %s warmup kicked off", provider.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[startup] sandbox warmup failed: %s", exc)


async def _startup_idle_session_reaper():
    """Background periodic reaping of idle persistent sandbox sessions (only opensandbox has reap_idle_sessions).

    With file/sandbox tools enabled in all modes, every ordinary conversation
    may bind a chat-level persistent container; the server-side 30min TTL is too
    long, so this reaps earlier per OPENSANDBOX_IDLE_REAP_S.
    Fire-and-forget, does not block startup; canceled by _shutdown_pools on
    process exit.
    """
    import asyncio

    try:
        from core.sandbox import get_sandbox_provider

        provider = get_sandbox_provider()
        reap = getattr(provider, "reap_idle_sessions", None)
        if reap is None:
            return

        async def _run():
            while True:
                await asyncio.sleep(120)
                try:
                    n = await reap()
                    if n:
                        logger.info("[idle-reaper] reaped %d idle sandbox session(s)", n)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("[idle-reaper] reap cycle failed: %s", e)

        global _idle_reaper_task
        _idle_reaper_task = asyncio.create_task(_run())
        from core.infra import runtime_state

        runtime_state.register("idle_session_reaper", _idle_reaper_task)
        logger.info("[startup] idle sandbox-session reaper started")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[startup] idle session reaper failed to start: %s", exc)


async def _startup_seed_page_config():
    """Seed default page_config content block if missing (idempotent)."""
    try:
        from core.content.content_blocks import enforce_ce_branding, seed_page_config_if_missing
        from core.db.engine import SessionLocal

        db = SessionLocal()
        try:
            inserted = seed_page_config_if_missing(db)
            if inserted:
                logger.info("[startup] default page_config seeded")
            if enforce_ce_branding(db):
                logger.info("[startup] CE page config normalized")
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] page_config seed failed: %s", exc)


async def _startup_seed_prompt_versions():
    """Seed the prompt version pool (default versions) if missing.

    Idempotent: only inserts versions that don't already exist.
    """
    try:
        from core.db.engine import SessionLocal
        from core.services import prompt_version_service as pvs

        db = SessionLocal()
        try:
            result = pvs.seed_from_filesystem(db=db)
            added = result.get("added") or []
            if added:
                logger.info("[startup] prompt versions seeded: %s", ", ".join(added))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] prompt versions seed failed: %s", exc)


async def _startup_seed_roles():
    """Seed default roles (部门成员 / IT管理员) if missing.

    Idempotent (by role name): existing roles are left untouched, so this only
    populates fresh deployments. When CE has no roles table, degrades entirely to a no-op.
    """
    try:
        from core.db.engine import SessionLocal
        from core.services.edition_startup import seed_default_roles

        db = SessionLocal()
        try:
            added = seed_default_roles(db)
            if added:
                logger.info("[startup] default roles seeded: %s", ", ".join(added))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] roles seed failed: %s", exc)


async def _startup_local_sidecars():
    """Spawn and verify MCP + script_runner sidecars in local profile.

    A local desktop process must not pass its health check while the MCP tools
    promised by the default plugins are absent.  Compose deployments are a
    no-op inside ``start_local_sidecars`` and keep their existing best-effort
    startup behavior.
    """
    try:
        from orchestration.local_subprocess import start_local_sidecars

        await start_local_sidecars()
    except Exception as exc:
        logger.error("[startup] local sidecars failed readiness: %s", exc)
        if settings.deploy.is_local:
            raise


async def _shutdown_local_sidecars():
    """Reap local-profile sidecar subprocesses on shutdown."""
    try:
        from orchestration.local_subprocess import stop_local_sidecars

        await stop_local_sidecars()
    except Exception as exc:
        logger.warning("[shutdown] local sidecars stop failed: %s", exc)


async def _startup_seed_mcp_servers():
    """Seed the built-in global MCP catalog if the DB has none (idempotent).

    On compose these rows come from alembic seed migrations; on the no-Docker
    local profile the DB is built by ``create_all()`` (alembic never runs), so the
    catalog would be empty. Only fires when no global built-in row exists, so it
    bootstraps a fresh install without touching alembic-seeded DBs or resurrecting
    admin-deleted rows. Missing table (CE edge) degrades to a no-op.
    """
    try:
        from core.db.engine import SessionLocal
        from core.services.mcp_service import (
            prune_removed_builtin_mcp_servers,
            seed_builtin_mcp_servers_if_empty,
        )

        db = SessionLocal()
        try:
            pruned = prune_removed_builtin_mcp_servers(db)
            if pruned:
                logger.info("[startup] unavailable built-in MCP rows pruned: %s", ", ".join(pruned))
            seeded = seed_builtin_mcp_servers_if_empty(db)
            if seeded:
                logger.info("[startup] built-in MCP catalog seeded: %s", ", ".join(seeded))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] MCP catalog seed failed: %s", exc)


async def _startup_seed_default_plugins():
    """Install the three credential-free plugins on first CE Compose boot.

    Local/desktop installs run the equivalent bootstrap in ``cli.py`` before
    the API is imported. Compose needs a database-backed marker so a user who
    later uninstalls one of the defaults does not have it resurrected after a
    container restart.
    """
    if settings.edition.edition != "ce" or settings.deploy.is_local:
        return

    from core.db.engine import SessionLocal
    from core.services.plugin_service import (
        DEFAULT_BOOTSTRAP_PLUGIN_SLUGS,
        ensure_default_plugins_bootstrapped,
    )

    db = SessionLocal()
    try:
        if ensure_default_plugins_bootstrapped(db):
            logger.info(
                "[startup] default plugins bootstrapped: %s",
                ", ".join(DEFAULT_BOOTSTRAP_PLUGIN_SLUGS),
            )
    except Exception as exc:
        logger.error("[startup] default plugin bootstrap failed: %s", exc)
        # These plugins are part of the advertised CE baseline. Do not report a
        # healthy web deployment with only a partial/default-missing toolset.
        raise
    finally:
        db.close()


async def _startup_recover_datasource_sidecars():
    """Recover configured DBHub/ES MCP sidecars without blocking API startup."""
    import asyncio

    async def _recover() -> None:
        try:
            from core.services.edition_startup import recover_datasource_sidecars

            result = await recover_datasource_sidecars()
            failures = [
                state.get("error", "unknown error")
                for key in ("dbhub", "es")
                if (state := result.get(key) or {}).get("desired") and not state.get("ok")
            ]
            if failures:
                logger.warning(
                    "[startup] datasource sidecar recovery incomplete: %s",
                    "; ".join(failures),
                )
                return
            recovered = [
                key for key in ("dbhub", "es") if (state := result.get(key) or {}).get("desired")
            ]
            if recovered:
                logger.info(
                    "[startup] datasource sidecars ready: %s",
                    ", ".join(recovered),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[startup] datasource sidecar recovery failed: %s", exc)

    app.state.datasource_sidecar_recovery_task = asyncio.create_task(_recover())


async def _shutdown_datasource_sidecar_recovery():
    import asyncio
    import contextlib

    task = getattr(app.state, "datasource_sidecar_recovery_task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _startup_seed_ontologies():
    """Seed or stage the latest built-in enterprise-risk Domain Pack.

    A fresh database activates the bundled version. Existing installations get
    a new immutable draft and keep their current active version until an
    administrator explicitly activates the update.
    """
    try:
        import json
        from pathlib import Path

        from core.db.engine import SessionLocal
        from core.services.ontology_service import OntologyService

        pack_path = (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "ontology_packs"
            / "enterprise_risk_v1.json"
        )
        with SessionLocal() as db:
            service = OntologyService(db)
            document = json.loads(pack_path.read_text(encoding="utf-8"))
            pack_id = str(document["pack_id"])
            version_name = str(document["version"])
            if service.repo.get_pack_version(pack_id, version_name) is not None:
                return
            is_fresh = service.repo.get_pack(pack_id) is None
            if not is_fresh and service.repo.get_working_draft(pack_id) is not None:
                logger.info(
                    "[startup] built-in ontology update deferred because a working draft exists: %s@%s",
                    pack_id,
                    version_name,
                )
                return
            service.create_version(
                document,
                actor_id="system_seed",
                activate=is_fresh,
            )
            if is_fresh:
                service.set_pack_flags(pack_id, is_enabled=True, is_default=True)
                logger.info(
                    "[startup] built-in ontology seeded and activated: %s@%s",
                    pack_id,
                    version_name,
                )
            else:
                logger.info(
                    "[startup] built-in ontology update staged as draft: %s@%s",
                    pack_id,
                    version_name,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[startup] ontology seed failed: %s", exc)


async def _startup_preload():
    """Pre-load caches and initialize MCP connection pool at startup."""
    import asyncio

    if not _startup_preload_enabled():
        logger.info("[startup] Preload disabled for current environment")
        return

    async def _run():
        import time

        start = time.monotonic()

        async def _run_sync(label: str, func: Callable[[], None]) -> None:
            try:
                await asyncio.to_thread(func)
                logger.info("[startup] %s loaded", label)
            except Exception as exc:
                logger.warning("[startup] %s preload failed: %s", label, exc)

        # 1. Pre-load prompt config (mtime-cached, ~0ms after first call)
        from prompts.prompt_config import load_prompt_config

        await _run_sync("Prompt config", load_prompt_config)

        # 2. Pre-load skill metadata
        def _load_skill_metadata() -> None:
            from core.agent_skills.loader import get_skill_loader

            loader = get_skill_loader()
            meta = loader.load_all_metadata()
            logger.info("[startup] Skill metadata loaded: %d skills", len(meta))

        await _run_sync("Skill metadata", _load_skill_metadata)

        # 2.5 Sync built-in skills into the unified sandbox skills dir so the
        #     single /workspace/skills bind mount exposes built-in + DB skills
        #     at the same in-sandbox path. DB skills materialize into the same
        #     dir on demand. Must run before sandboxes are created/used.
        def _sync_builtin_skills() -> None:
            from core.agent_skills.config import sync_builtin_skills_to_sandbox_dir

            n = sync_builtin_skills_to_sandbox_dir()
            logger.info("[startup] Built-in skills synced to sandbox dir: %d", n)
            # Pre-create the Yida workspace for the script-runner shared sandbox
            # (0777: the runner is uid 1001 and needs write, backend is 1000).
            # If the directory doesn't exist, the compose volume mount gets
            # created by dockerd as root and the runner can't write into it →
            # backend must create it first. opensandbox/cube don't use this one.
            try:
                from core.sandbox._common import yida_shared_workspace_dir

                p = yida_shared_workspace_dir()
                p.mkdir(parents=True, exist_ok=True)
                p.chmod(0o777)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[startup] pre-create yida shared workspace failed: %s", exc)

        await _run_sync("Built-in skills sandbox sync", _sync_builtin_skills)

        # 3. Pre-load public DB capability overlay so first chat avoids
        #    scanning admin_skills/admin_mcp_servers on the request path.
        def _warm_runtime_catalog() -> None:
            from core.config.catalog_runtime import warmup_runtime_catalog_cache

            warmup_runtime_catalog_cache()
            logger.info("[startup] Runtime catalog DB overlay warmed")

        await _run_sync("Runtime catalog", _warm_runtime_catalog)

        # 4. Initialize MCP connection pool (the big one — 1-7s savings)
        if _mcp_warmup_enabled():
            try:
                from core.llm.agent_factory import warmup_mcp_tools

                await warmup_mcp_tools()
            except Exception as exc:
                logger.warning("[startup] MCP pool initialization failed: %s", exc)
        else:
            logger.info("[startup] MCP pool warmup skipped for current environment")

        # 5. Pre-load DB prompt parts so first chat skips DB query
        from prompts.prompt_runtime import warmup_prompt_cache

        await _run_sync("Prompt cache", warmup_prompt_cache)

        # 6. Idempotent backfill of navigation entries that were added to
        #    DEFAULT_PAGE_CONFIG after this env first seeded its page_config
        #    row. See _NAV_BACKFILL_ENTRIES in content_blocks.py — append a
        #    new entry there each time we add a sidebar feature.
        def _backfill_nav():
            from core.content.content_blocks import backfill_navigation_entries
            from core.db.engine import SessionLocal

            db = SessionLocal()
            try:
                changed = backfill_navigation_entries(db)
                if changed:
                    logger.info(
                        "[startup] page_config navigation backfilled (%d field(s))", changed
                    )
            finally:
                db.close()

        await _run_sync("Page config nav backfill", _backfill_nav)

        elapsed = time.monotonic() - start
        logger.info("[startup] Preload complete in %.2fs", elapsed)

    asyncio.create_task(_run())


async def _startup_automation_scheduler():
    """Start the automation scheduler for timed task execution."""
    if not _env_flag("AUTOMATION_ENABLED", True):
        logger.info("[startup] Automation scheduler disabled")
        return
    try:
        from orchestration.schedulers.automation_scheduler import AutomationScheduler

        global _automation_scheduler
        _automation_scheduler = AutomationScheduler()
        await _automation_scheduler.start()
        from core.infra import runtime_state

        runtime_state.register("automation_scheduler", _automation_scheduler)
    except Exception as exc:
        logger.warning("[startup] Automation scheduler failed to start: %s", exc)


async def _startup_channel_manager():
    """Start the inbound channel bot long connections (owner service-account model)."""
    if not _env_flag("CHANNEL_BOTS_ENABLED", True):
        logger.info("[startup] Channel bots disabled")
        return
    try:
        from core.channels.manager import get_manager

        await get_manager().start_all()
        from core.infra import runtime_state

        runtime_state.register("channel_manager", get_manager())
    except Exception as exc:
        logger.warning("[startup] Channel manager failed to start: %s", exc)


_automation_scheduler = None
_distillation_scheduler = None
_idle_reaper_task = None


async def _startup_distillation_scheduler():
    """Start the daily skill-distillation cron scheduler."""
    try:
        from core.services.edition_startup import create_distillation_scheduler

        global _distillation_scheduler
        _distillation_scheduler = create_distillation_scheduler()
        if _distillation_scheduler is None:
            return
        await _distillation_scheduler.start()
        from core.infra import runtime_state

        runtime_state.register("distillation_scheduler", _distillation_scheduler)
    except Exception as exc:
        logger.warning("[startup] Distillation cron scheduler failed to start: %s", exc)


async def _startup_recover_persona_distill_jobs():
    """After a process restart, set orphan persona distillation jobs (queued/running) to failed."""
    try:
        from core.services.edition_startup import recover_persona_distill_jobs

        n = recover_persona_distill_jobs()
        if n:
            logger.info("[startup] persona distill: marked %d orphan job(s) as failed", n)
    except Exception as exc:
        logger.warning("[startup] persona distill orphan recovery failed: %s", exc)


async def _startup_warmup_memory():
    """Prewarm the mem0 / Milvus connections in the background so the first request avoids cold-connection handshake latency.

    Fire-and-forget: does not block startup; failure does not affect service start.
    """
    if not settings.memory.enabled:
        return
    import asyncio as _asyncio

    async def _warmup() -> None:
        try:
            from core.memory.service import _get_memory  # type: ignore[attr-defined]

            # _get_memory is thread-safe lazy loading; the first call establishes the Milvus + Neo4j connections
            instance = _get_memory()
            if instance is not None:
                logger.info("[startup] memory (mem0/milvus) connection warmed up")
            else:
                logger.info("[startup] memory enabled but initialization returned None")
        except Exception as exc:
            logger.warning("[startup] memory warmup failed: %s", exc)

    try:
        _asyncio.create_task(_warmup())
    except RuntimeError:
        # no running loop — should not happen in startup event
        logger.debug("[startup] no event loop for memory warmup")


async def _shutdown_pools():
    """Close MCP connection pool, KB HTTP server, automation scheduler, and Redis on shutdown."""
    # Stop automation scheduler
    global _automation_scheduler
    if _automation_scheduler is not None:
        try:
            await _automation_scheduler.stop()
        except Exception as e:
            logger.warning("automation_scheduler_shutdown_error", error=str(e))
    # Stop distillation scheduler
    global _distillation_scheduler
    if _distillation_scheduler is not None:
        try:
            await _distillation_scheduler.stop()
        except Exception as e:
            logger.warning("distillation_scheduler_shutdown_error", error=str(e))
    # Stop idle sandbox-session reaper
    global _idle_reaper_task
    if _idle_reaper_task is not None:
        try:
            _idle_reaper_task.cancel()
        except Exception as e:
            logger.warning("idle_reaper_shutdown_error", error=str(e))
    try:
        from core.llm.mcp_pool import MCPConnectionPool

        await MCPConnectionPool.get_instance().shutdown()
    except Exception as e:
        logger.warning("mcp_pool_shutdown_error", error=str(e))

    try:
        from core.infra.redis import close_redis

        await close_redis()
    except Exception as e:
        logger.warning("redis_shutdown_error", error=str(e))


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
