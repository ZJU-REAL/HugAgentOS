"""Catalog HTTP contract: listing buckets, per-user toggle, and kind strictness."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.v1 import catalog
from core.auth.backend import UserContext, get_current_user
from core.capabilities.catalog import common as catalog_common
from core.capabilities.catalog.loader import invalidate_catalog_cache
from core.db.engine import get_db


@pytest.fixture
def client(db_session, tmp_path, monkeypatch):
    from api.middleware.error_handler import setup_error_handlers
    from core.db.models.identity import UserShadow

    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setattr(catalog_common, "CATALOG_PATH", tmp_path / "catalog.json")
    invalidate_catalog_cache()

    async def in_test_session(function, *args):
        return function(db_session, *args)

    monkeypatch.setattr(catalog, "run_in_session", in_test_session)
    db_session.add(UserShadow(user_id="catalog-user", user_center_id="catalog-user", username="catalog-user"))
    db_session.commit()

    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(catalog.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="catalog-user", user_center_id="catalog-user", username="catalog-user"
    )
    yield TestClient(app)
    invalidate_catalog_cache()


def test_catalog_lists_every_bucket_and_toggles_a_skill(client):
    response = client.get("/v1/catalog")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) >= {"skills", "agents", "mcp", "kb"}
    assert data["skills"], "built-in skills should be listed"
    skill_id = data["skills"][0]["id"]

    toggled = client.patch(f"/v1/catalog/skills/{skill_id}", json={"enabled": False})

    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["data"]["enabled"] is False


def test_unsupported_kind_is_not_routed(client):
    # The kind segment is a strict URL convertor, so unknown kinds never reach the handler.
    response = client.patch("/v1/catalog/unsupported/some-id", json={"enabled": True})
    assert response.status_code in (404, 405)


def test_toggle_requires_a_field(client):
    response = client.patch("/v1/catalog/skills/any-skill", json={})
    assert response.status_code == 400
