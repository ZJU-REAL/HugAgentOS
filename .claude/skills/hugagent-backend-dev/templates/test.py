"""Test template.

Replace ${Feature}, ${feature} with actual names.
Create as src/backend/tests/features/${feature}/test_${feature}.py (core domains: tests/<domain>/test_*.py).
Run:  PYTHONPATH=src/backend pytest src/backend/tests/features/${feature} -q

Rules:
  - Test behaviour at a real boundary (service against a real DB session, or HTTP through the router);
    do not assert internal call order
  - The shared `db_session` fixture (tests/conftest.py) gives a fresh database per test (SQLite unless
    TEST_DATABASE_URL points at PostgreSQL); schema changes should pass on both
  - Write the failing test first; never weaken an assertion to make it pass
"""

import pytest
from core.infra.exceptions import BadRequestError, ResourceNotFoundError, ResourceOwnershipError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from features.${feature}.service import ${Feature}Service


@pytest.fixture
def service(db_session):
    return ${Feature}Service(db_session)


def test_create_and_list(service):
    created = service.create("user_a", "  周报  ")
    assert created.name == "周报"
    items, total = service.list_items("user_a", 1, 20)
    assert total == 1 and items[0].id == created.id


def test_rejects_blank_name(service):
    with pytest.raises(BadRequestError):
        service.create("user_a", "   ")


def test_other_users_cannot_read_or_delete(service):
    item = service.create("user_a", "私有")
    with pytest.raises(ResourceOwnershipError):
        service.get_owned(item.id, "user_b")
    with pytest.raises(ResourceOwnershipError):
        service.delete(item.id, "user_b")


def test_soft_delete_hides_item(service):
    item = service.create("user_a", "待删")
    service.delete(item.id, "user_a")
    with pytest.raises(ResourceNotFoundError):
        service.get_owned(item.id, "user_a")
    assert service.list_items("user_a", 1, 20)[1] == 0


# --- HTTP boundary: mount only this router, override auth and DB --------------------------------


@pytest.fixture
def client(db_session, monkeypatch):
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from core.infra.exceptions import AppException
    from features.${feature}.api.${feature}s import router
    from fastapi.responses import JSONResponse
    from tests.db_support import share_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    # async routes reach the database through run_in_session, not get_db
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="user_a", user_center_id="uc_a", username="a")

    @app.exception_handler(AppException)
    async def _envelope(_, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})

    return TestClient(app)


def test_http_create_returns_envelope(client):
    resp = client.post("/v1/${feature}s", json={"name": "周报"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 10001
    assert body["data"]["name"] == "周报"
