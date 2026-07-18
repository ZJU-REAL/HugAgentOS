"""
PyTest configuration and fixtures for all tests.
"""

import pytest
import os
import tempfile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from core.db.engine import Base

@pytest.fixture(scope="session")
def test_database_url():
    """Test database URL."""
    # Default to the system temp dir: a relative path would leave a stray test.db in pytest's CWD (repo root, etc.)
    return os.getenv("TEST_DATABASE_URL", f"sqlite:///{tempfile.gettempdir()}/hugagent_test.db")


@pytest.fixture(scope="function")
def db_session(test_database_url):
    """Create a clean database session for each test."""
    engine = create_engine(test_database_url)
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    session = Session()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


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
