"""Concurrent pytest sessions never share a default SQLite database."""
import os

import pytest

from tests import conftest


def test_default_databases_are_private_to_this_session(session_db_dir, test_database_url):
    assert session_db_dir == conftest.SESSION_DB_DIR
    assert f"hugagent-tests-{os.getpid()}-" in os.path.basename(session_db_dir)
    if os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL overrides the session database")
    assert test_database_url == f"sqlite:///{session_db_dir}/hugagent_test.db"


def test_application_database_defaults_into_the_session_dir(session_db_dir):
    if conftest.DATABASE_URL_PRESET:
        pytest.skip("DATABASE_URL was set explicitly")
    from core.config.settings import settings

    assert settings.db.url == f"sqlite:///{session_db_dir}/hugagent.db"
    assert settings.db.sqlite_fallback_url.startswith(f"sqlite:///{session_db_dir}/")
