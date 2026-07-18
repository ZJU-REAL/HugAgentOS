"""Test template.

Replace ${Feature}, ${feature} with actual names.
Create as tests/test_${feature}.py.
Run: PYTHONPATH=src/backend pytest src/backend/tests/test_${feature}.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ${Feature}
from core.db.repository import ${Feature}Repository
from core.services.${feature}_service import ${Feature}Service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """In-memory SQLite session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def service(db_session):
    """${Feature}Service with test DB session."""
    return ${Feature}Service(db_session)


@pytest.fixture
def sample_data():
    """Sample creation data."""
    return {
        "user_id": "test_user_001",
        "name": "Test ${Feature}",
        "description": "Test description",
    }


# ---------------------------------------------------------------------------
# Repository Tests
# ---------------------------------------------------------------------------

class TestRepository:
    def test_create(self, db_session, sample_data):
        repo = ${Feature}Repository(db_session)
        item = repo.create({
            "id": "${feature}_test001",
            **sample_data,
        })
        assert item.id == "${feature}_test001"
        assert item.name == sample_data["name"]
        assert item.deleted_at is None

    def test_get_by_id(self, db_session):
        repo = ${Feature}Repository(db_session)
        repo.create({"id": "${feature}_001", "user_id": "u1", "name": "A"})
        found = repo.get_by_id("${feature}_001")
        assert found is not None
        assert found.name == "A"

    def test_get_by_id_respects_soft_delete(self, db_session):
        repo = ${Feature}Repository(db_session)
        repo.create({"id": "${feature}_del", "user_id": "u1", "name": "D"})
        repo.soft_delete("${feature}_del")
        assert repo.get_by_id("${feature}_del") is None

    def test_list_by_user_pagination(self, db_session):
        repo = ${Feature}Repository(db_session)
        for i in range(5):
            repo.create({"id": f"${feature}_{i}", "user_id": "u1", "name": f"Item {i}"})
        items, total = repo.list_by_user("u1", page=1, page_size=3)
        assert total == 5
        assert len(items) == 3


# ---------------------------------------------------------------------------
# Service Tests
# ---------------------------------------------------------------------------

class TestService:
    def test_create(self, service, sample_data):
        item = service.create(**sample_data)
        assert item.name == sample_data["name"]
        assert item.user_id == sample_data["user_id"]
        assert item.id.startswith("${feature}_")

    def test_get_item_ownership(self, service):
        item = service.create(user_id="u1", name="Mine")
        # Owner can access
        assert service.get_item(item.id, "u1") is not None
        # Non-owner raises
        with pytest.raises(Exception):
            service.get_item(item.id, "u2")

    def test_delete(self, service):
        item = service.create(user_id="u1", name="ToDelete")
        assert service.delete(item.id, "u1") is True
        assert service.get_item(item.id, "u1") is None

    def test_ensure_idempotent(self, service):
        item1 = service.create(user_id="u1", name="First")
        item2 = service.ensure(item1.id, "u1")
        assert item1.id == item2.id
