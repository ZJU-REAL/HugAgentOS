"""Persistent application registry in a database separate from platform state."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

metadata = MetaData()
applications = Table(
    "hosted_applications",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("title", String(200), nullable=False),
    Column("site_id", String(64), unique=True),
    Column("tables", JSON, nullable=False),
    Column("tools", JSON, nullable=False),
    Column("token_hash", String(64)),
    Column("mcp_enabled", Boolean, nullable=False),
    Column("mcp_version", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
operations = Table(
    "hosted_application_operations",
    metadata,
    Column("app_id", String(32), primary_key=True),
    Column("request_key", String(64), primary_key=True),
    Column("body_hash", String(64), nullable=False),
    Column("result", JSON, nullable=False),
)
mcp_deployments = Table(
    "hosted_mcp_deployments",
    metadata,
    Column("app_id", String(32), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("tools", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Platform project IDs are references across two databases, not cross-store FKs.
application_sources = Table(
    "hosted_application_sources",
    metadata,
    Column("app_id", String(32), primary_key=True),
    Column("project_id", String(64), nullable=False, unique=True),
)


@lru_cache(maxsize=1)
def application_engine() -> Engine:
    from core.config.application_hosting import application_hosting_settings
    from core.config.settings import settings

    url = application_hosting_settings.database_url
    if not url:
        if not settings.deploy.is_local:
            raise HTTPException(
                503, "APPLICATION_DATABASE_URL must configure a separate data database"
            )
        root = settings.storage.root
        root.mkdir(parents=True, exist_ok=True)
        url = "sqlite:///" + str((root / "applications.sqlite").resolve())
    from core.db.engine import DATABASE_URL
    from sqlalchemy.engine import make_url

    target, platform = make_url(url), make_url(DATABASE_URL)
    same_database = (target.get_backend_name(), target.host, target.port, target.database) == (
        platform.get_backend_name(),
        platform.host,
        platform.port,
        platform.database,
    )
    if same_database:
        raise HTTPException(503, "Application data must use a separate database")
    engine = create_engine(url, pool_pre_ping=True)
    if target.get_backend_name() == "sqlite":
        from core.db.engine import apply_sqlite_concurrency_pragmas

        apply_sqlite_concurrency_pragmas(engine, settings.db.pool_timeout)
    if target.get_backend_name() == "postgresql":
        try:
            with engine.connect() as connection:
                if connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
                    raise HTTPException(503, "Application runtime requires a non-superuser owner")
        except HTTPException:
            engine.dispose()
            raise
        except SQLAlchemyError:
            engine.dispose()
            raise HTTPException(503, "Application data service is unavailable") from None
    return engine


def initialize_store(engine: Engine) -> None:
    """Explicit provisioning entry point; runtime requests never create registry tables."""
    from core.services.application_history import collection_history
    metadata.create_all(engine)


def initialize_local_store() -> None:
    """Provision the managed local SQLite store at startup, never cloud databases."""
    from core.auth.desktop_bridge import bridge_enabled
    from core.config.application_hosting import application_hosting_settings
    from core.config.settings import settings

    if (
        not settings.deploy.is_local
        or bridge_enabled()
        or application_hosting_settings.database_url
    ):
        return
    initialize_store(application_engine())


def require_store(engine: Engine) -> None:
    from core.services.application_history import collection_history
    try:
        ready = all(
            inspect(engine).has_table(table.name) for table in (applications, application_sources, metadata.tables["hosted_collection_history"])
        )
    except SQLAlchemyError:
        raise HTTPException(503, "Application data service is unavailable")
    if not ready:
        raise HTTPException(
            503,
            "Application database requires provisioning or upgrade; run application_hosting_setup",
        )


def owned_application(connection, app_id: str, user_id: str) -> dict:
    app = (
        connection.execute(
            select(applications).where(
                applications.c.id == app_id,
                applications.c.user_id == user_id,
            )
        )
        .mappings()
        .first()
    )
    if app is None:
        raise HTTPException(404, "Application not found")
    return dict(app)


@contextmanager
def write_connection(engine):
    """Own the transaction for registry locks, DDL and writes."""
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield connection
