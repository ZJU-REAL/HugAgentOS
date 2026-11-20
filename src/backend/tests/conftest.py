"""
PyTest configuration and fixtures for all tests.
"""

import pytest
import os
import shutil
import tempfile

# Every pytest session gets its own SQLite files. A fixed name in the system
# temp dir is shared by concurrent runs (other checkouts, other sessions), which
# drop each other's tables mid-test. Set before core.config reads the env;
# explicit DATABASE_URL / SQLITE_FALLBACK_URL / TEST_DATABASE_URL still win.
SESSION_DB_DIR = tempfile.mkdtemp(prefix=f"hugagent-tests-{os.getpid()}-")
DATABASE_URL_PRESET = "DATABASE_URL" in os.environ
os.environ.setdefault("DATABASE_URL", f"sqlite:///{SESSION_DB_DIR}/hugagent.db")
os.environ.setdefault("SQLITE_FALLBACK_URL", f"sqlite:///{SESSION_DB_DIR}/hugagent_dev.db")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from core.db.engine import Base


def pytest_unconfigure(config):
    from core.harness.observation_writer import flush_observations

    flush_observations()
    shutil.rmtree(SESSION_DB_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def session_db_dir():
    return SESSION_DB_DIR


@pytest.fixture(scope="session")
def test_database_url():
    """Test database URL, private to this pytest session unless TEST_DATABASE_URL is set."""
    return _default_test_database_url()


def _default_test_database_url():
    return os.getenv("TEST_DATABASE_URL", f"sqlite:///{SESSION_DB_DIR}/hugagent_test.db")


@pytest.fixture(scope="session")
def db_engine():
    """One engine and one schema for the whole run, instead of create_all/drop_all per test."""
    engine = create_engine(_default_test_database_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _empty_all_tables(engine):
    """Delete every row, children first. Commits stay real, so the code under test sees an
    ordinary Engine-bound session (``db.get_bind()`` is the engine, as in production)."""
    tables = list(reversed(Base.metadata.sorted_tables))
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "TRUNCATE " + ", ".join(f'"{t.name}"' for t in tables) + " RESTART IDENTITY CASCADE")
        else:
            for table in tables:
                connection.execute(table.delete())
            # AUTOINCREMENT counters restart too, as they did when each test rebuilt the schema.
            if connection.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").first():
                connection.exec_driver_sql("DELETE FROM sqlite_sequence")


@pytest.fixture(scope="function")
def db_session(request, test_database_url):
    """A session on the shared test database; every table is emptied when the test ends.

    A module that overrides ``test_database_url`` gets a private engine for that test instead.
    """
    if test_database_url == _default_test_database_url():
        engine = request.getfixturevalue("db_engine")
        private = False
    else:
        engine = create_engine(test_database_url)
        Base.metadata.create_all(engine)
        private = True
    session = sessionmaker(bind=engine)()

    yield session

    session.close()
    if private:
        Base.metadata.drop_all(engine)
        engine.dispose()
    else:
        _empty_all_tables(engine)


@pytest.fixture
def memory_sessions(monkeypatch):
    """A session factory on a fresh in-memory SQLite, also installed as ``core.db.engine.SessionLocal``.

    For tests whose code opens its own sessions (threads, background steps). Modules that
    hold their own ``SessionLocal`` reference: ``tests.db_support.patch_session_local``.
    """
    from tests.db_support import memory_engine, session_factory, share_factory

    engine = memory_engine()
    factory = session_factory(engine)
    share_factory(monkeypatch, factory)
    yield factory
    engine.dispose()


@pytest.fixture(scope="module")
def test_client():
    """Create FastAPI test client."""
    from api.app import app

    return TestClient(app)


@pytest.fixture(scope="function")
def test_user():
    """Mock test user."""
    return {
        "user_id": "test_user_123",
        "user_center_id": "test_center_001",
        "username": "Test User",
        "email": "test@example.com"
    }


@pytest.fixture(scope="function")
def auth_headers(test_user):
    """Mock authentication headers."""
    # In real tests, generate actual JWT token
    return {"Authorization": "Bearer test_token_123"}


@pytest.fixture(scope="session", autouse=True)
def product_tool_contributions():
    """Compose product tools for tests invoking the core factory without ASGI."""
    from features import register_product_tools, register_package_sources, register_resource_owners, register_run_capabilities
    register_product_tools()
    from features import register_catalog_options
    register_catalog_options()
    register_package_sources()
    register_resource_owners()
    register_run_capabilities()
    from features import register_model_credentials
    register_model_credentials()
    from core.harness.registry import load_models
    from features.installed import INSTALLED_FEATURES
    load_models(INSTALLED_FEATURES)
    from integrations import register_credentials
    register_credentials()
