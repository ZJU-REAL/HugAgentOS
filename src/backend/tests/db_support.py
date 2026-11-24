"""Point the sessions async code opens at a test's database.

Async routes and workers never take ``Depends(get_db)``; their database steps
run through ``core.db.threads.run_in_session``, which opens
``core.db.engine.SessionLocal``. A test that overrides ``get_db`` overrides
this too, so both kinds of route see the same data.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import sessionmaker


class _Borrowed:
    """The test's session, handed out as if new; closing it leaves the test's copy open."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def share_session(monkeypatch, session):
    """Every step reuses the test's own session, left open like the ``get_db`` override."""
    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: _Borrowed(session))


def share_factory(monkeypatch, factory):
    """Every step opens its own session from the test's factory."""
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)


def share_bind(monkeypatch, bind):
    """Every step opens its own session on the test's engine."""
    share_factory(monkeypatch, sessionmaker(bind=bind))


# ---------------------------------------------------------------------------
# Test databases. Most tests want "a fresh SQLite with every table, and the
# code under test opening its sessions on it"; these build exactly that.
# ---------------------------------------------------------------------------

def memory_engine(*, tables=None):
    """An in-memory SQLite shared by every thread (one StaticPool connection), schema created."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from core.db.engine import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=tables)
    return engine


def file_engine(path, *, tables=None, timeout=None):
    """A SQLite file database, schema created: for code that needs a connection per thread."""
    from sqlalchemy import create_engine

    from core.db.engine import Base

    connect_args = {"check_same_thread": False}
    if timeout is not None:
        connect_args["timeout"] = timeout
    engine = create_engine(f"sqlite:///{path}", connect_args=connect_args)
    Base.metadata.create_all(engine, tables=tables)
    return engine


def session_factory(engine):
    """Sessions that keep loaded attributes after commit, like the app's SessionLocal."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def patch_session_local(monkeypatch, factory, *modules):
    """Point ``core.db.engine.SessionLocal`` and each module's own ``SessionLocal`` at ``factory``.

    ``modules`` are module objects or dotted module paths that did
    ``from core.db.engine import SessionLocal`` and so hold their own reference.
    """
    share_factory(monkeypatch, factory)
    for module in modules:
        if isinstance(module, str):
            monkeypatch.setattr(module + ".SessionLocal", factory)
        else:
            monkeypatch.setattr(module, "SessionLocal", factory)
    return factory


def run_alembic(repo_root: Path, database_url: str, *args: str) -> None:
    """Run ``alembic <args>`` against ``database_url`` from the repository root."""
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
