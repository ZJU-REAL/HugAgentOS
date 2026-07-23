"""Database configuration and session management."""

import logging
from typing import Generator

from core.config.settings import settings
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str:
    """Resolve database URL with a dev-safe fallback."""
    url = settings.db.url
    if url.startswith("postgresql://"):
        try:
            import psycopg2  # type: ignore # noqa: F401
        except ModuleNotFoundError:
            fallback = settings.db.sqlite_fallback_url
            logger.warning(
                "psycopg2 is not installed, fallback to SQLite for local run. fallback=%s",
                fallback,
            )
            return fallback
    return url


# Database URL from environment variable
DATABASE_URL = _resolve_database_url()

engine_kwargs = {
    "pool_pre_ping": True,
    "echo": settings.db.echo,
}

if DATABASE_URL.startswith("sqlite://"):
    # SQLite-specific options for local development/testing.
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    # Streaming requests can hold connections for a long time. NullPool avoids
    # exhausting a small QueuePool in local SQLite dev mode.
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_size"] = settings.db.pool_size
    engine_kwargs["max_overflow"] = settings.db.pool_max_overflow
    engine_kwargs["pool_timeout"] = settings.db.pool_timeout

# Create engine
engine = create_engine(DATABASE_URL, **engine_kwargs)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """
    Dependency function to get database session.

    Usage:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize or reconcile the database schema for the active edition."""
    if settings.edition.edition == "ce":
        from core.db.edition_tables import ce_reconcile_schema

        report = ce_reconcile_schema(engine)
        if any(report.values()):
            logger.info("CE database schema reconciled: %s", report)
        return
    # Import models so SQLAlchemy metadata is populated before create_all().
    from core.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
