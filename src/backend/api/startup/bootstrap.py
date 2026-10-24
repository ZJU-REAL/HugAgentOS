"""Application lifecycle operations."""
from core.llm import prompt_versions as prompt_versions_module
import os
import sys
from core.config.settings import settings
from core.infra.logging import get_logger
logger = get_logger(__name__)

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
        from core.auth.local_user_bootstrap import ensure_ce_default_admin

        db = SessionLocal()
        try:
            user_id, created = ensure_ce_default_admin(db)
            if created:
                logger.info("[startup] CE default administrator created: %s", user_id)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] CE default administrator seed failed: %s", exc)

async def _startup_prepare_artifact_records():
    """Split a legacy monolithic artifact index, or restore records from OSS.

    One-time and idempotent. It runs here rather than lazily on first lookup so
    that no user request ever pays for it — the monolithic index reached 12.9 MB
    in production and rewriting it per artifact was blocking the whole backend.
    """
    try:
        import asyncio

        from core.files.artifacts.store import prepare_records

        written = await asyncio.to_thread(prepare_records)
        if written:
            logger.info("[startup] artifact records prepared: %d written", written)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logger.warning("[startup] artifact record migration skipped: %s", exc)

async def _startup_seed_page_config():
    """Seed default page_config content block if missing (idempotent)."""
    try:
        from core.files.content.branding import enforce_ce_branding, seed_page_config_if_missing
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
        from core.llm import prompt_versions as pvs

        db = SessionLocal()
        try:
            result = prompt_versions_module.seed_from_filesystem(db=db)
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
        from orchestration.workers.edition_startup import seed_default_roles

        db = SessionLocal()
        try:
            added = seed_default_roles(db)
            if added:
                logger.info("[startup] default roles seeded: %s", ", ".join(added))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[startup] roles seed failed: %s", exc)
