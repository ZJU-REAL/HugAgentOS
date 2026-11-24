"""Fixtures shared by the API tests."""
import pytest

from core.db.models import UserShadow


@pytest.fixture
def alice(db_session):
    u = UserShadow(user_id="user_alice", username="Alice", email="alice@example.com")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def bob(db_session):
    u = UserShadow(user_id="user_bob", username="Bob", email="bob@example.com")
    db_session.add(u)
    db_session.commit()
    return u
