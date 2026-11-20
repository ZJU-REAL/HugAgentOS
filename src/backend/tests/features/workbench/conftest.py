"""Fixtures shared by the workbench route tests."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from tests.db_support import share_session


@pytest.fixture()
def client(db_session, monkeypatch):
    from features.workbench.api.workbench import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice", user_center_id="alice", username="Alice"
    )
    return TestClient(app)
