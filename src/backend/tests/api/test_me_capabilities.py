"""GET /v1/me exposes the effective permission fields, including to API-key callers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import core.db.models  # noqa: F401  (register every table on Base.metadata)
from api.routes.v1.users import router as users_router
from core.auth.api_keys import ApiKeyService
from core.auth.capabilities import (
    BOOL_CAPABILITY_DEFAULTS,
    PAGE_ADMIN_FLAGS,
    account_capability_payload,
)
from core.db.engine import get_db
from core.db.models import LocalUser, UserShadow
from tests.db_support import share_session
from tests.db_support import memory_engine


@pytest.fixture
def db():
    engine = memory_engine()
    session = sessionmaker(bind=engine)()
    session.add(
        UserShadow(
            user_id="u1",
            user_center_id="u1",
            username="alice",
            extra_data={"can_use_api_key": True, "can_add_skill": True, "allowed_apps": ["a1"]},
        )
    )
    session.add(LocalUser(user_id="u1", nickname="alice", password_hash="x", status="active"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(users_router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_payload_has_every_permission_field(db):
    payload = account_capability_payload(db, "u1")

    expected = {"allowed_apps", *BOOL_CAPABILITY_DEFAULTS, *PAGE_ADMIN_FLAGS}
    assert set(payload) == expected
    assert payload["allowed_apps"] == ["a1"]
    assert payload["can_use_api_key"] is True
    assert payload["can_add_skill"] is True
    assert payload["can_system_config"] is False
    assert all(isinstance(payload[k], bool) for k in expected - {"allowed_apps"})


def test_super_admin_implies_page_admin_flags(db):
    shadow = db.get(UserShadow, "u1")
    shadow.extra_data = {**shadow.extra_data, "role": "super_admin"}
    db.commit()

    payload = account_capability_payload(db, "u1")

    assert payload["can_system_config"] is True
    assert payload["can_content_manage"] is True


def test_me_returns_permissions_for_api_key_caller(db, monkeypatch):
    share_session(monkeypatch, db)
    _, raw = ApiKeyService(db).create_key("u1", name="probe")

    resp = _client(db).get("/v1/me", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["user_id"] == "u1"
    assert data["can_use_api_key"] is True
    assert data["can_add_skill"] is True
    assert data["allowed_apps"] == ["a1"]
    for flag in (*BOOL_CAPABILITY_DEFAULTS, *PAGE_ADMIN_FLAGS):
        assert flag in data


def test_openapi_schema_lists_every_permission_flag():
    from api.openapi_data_schemas import _CAPABILITY_FLAGS, DATA_COMPONENTS

    assert set(_CAPABILITY_FLAGS) == set(BOOL_CAPABILITY_DEFAULTS)
    props = DATA_COMPONENTS["CurrentUserInfo"]["properties"]
    assert {"allowed_apps", *BOOL_CAPABILITY_DEFAULTS} <= set(props)
