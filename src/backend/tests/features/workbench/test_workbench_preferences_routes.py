"""Public navigation preference behavior through authenticated HTTP."""

import pytest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.db_support import share_factory, share_session

BASE = "/v1/workbench/me/navigation-preferences"


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


def test_navigation_is_independent_durable_conditional_and_keeps_unavailable_references(client):
    initial = client.get(BASE).json()["data"]
    assert initial == {"revision": 0, "pinned": None, "order": [], "environment": "web"}
    body = {
        "revision": 0,
        "pinned": ["builtin:projects", "external:temporarily_missing"],
        "order": ["external:temporarily_missing", "builtin:projects"],
    }
    saved = client.put(BASE, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] == 1
    assert client.put(BASE, json=body).status_code == 409
    assert client.get(BASE).json()["data"]["pinned"] == body["pinned"]


def test_user_environment_isolation_and_reset_do_not_change_other_preferences(client, db_session):
    from core.db.models import UserShadow

    db_session.add(
        UserShadow(
            user_id="alice",
            username="Alice",
            extra_data={"theme": "dark", "preferences": {"language": "en"}},
        )
    )
    db_session.commit()
    body = {"revision": 0, "pinned": [], "order": ["plugin:install_123:missing"]}
    assert client.put(BASE + "?environment=local", json=body).status_code == 200
    assert client.get(BASE).json()["data"]["revision"] == 0
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="bob", user_center_id="bob", username="Bob"
    )
    assert client.get(BASE + "?environment=local").json()["data"]["revision"] == 0
    assert (
        client.put(
            BASE + "?environment=local", json={**body, "pinned": ["builtin:automation"]}
        ).status_code
        == 200
    )
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice", user_center_id="alice", username="Alice"
    )
    assert client.get(BASE + "?environment=local").json()["data"]["pinned"] == []
    assert (
        client.put(
            BASE + "?environment=local", json={"revision": 1, "pinned": None, "order": []}
        ).status_code
        == 200
    )
    assert client.get(BASE + "?environment=local").json()["data"]["pinned"] is None
    db_session.expire_all()
    assert db_session.get(UserShadow, "alice").extra_data == {
        "theme": "dark",
        "preferences": {"language": "en"},
    }


@pytest.mark.parametrize("revision", [0, 1])
def test_two_devices_same_revision_have_one_winner(client, db_session, revision, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import sessionmaker

    if revision:
        assert client.put(BASE, json={"revision": 0, "pinned": [], "order": []}).status_code == 200
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)
    barrier = Barrier(2)

    def write(key):
        barrier.wait()
        return client.put(
            BASE, json={"revision": revision, "pinned": [key], "order": []}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "builtin:projects")
        second = pool.submit(write, "external:missing")
        assert sorted([first.result(), second.result()]) == [200, 409]
    final = client.get(BASE).json()["data"]
    assert final["revision"] == revision + 1
    assert final["pinned"] in [["builtin:projects"], ["external:missing"]]


@pytest.mark.parametrize(
    "body",
    [
        {"revision": True, "pinned": [], "order": []},
        {"revision": 0, "pinned": ["https://evil.example"], "order": []},
        {"revision": 0, "pinned": ["builtin:projects", "builtin:projects"], "order": []},
        {"revision": 0, "pinned": [], "order": [], "user_id": "bob"},
        {"revision": 0, "pinned": [], "order": ["builtin:settings"] * 201},
    ],
)
def test_invalid_or_forged_preferences_are_rejected_without_echoing_values(client, body):
    result = client.put(BASE, json=body)
    assert result.status_code == 422
    assert "https://evil.example" not in result.text
    assert client.get(BASE).json()["data"]["revision"] == 0


def test_environment_and_json_boundaries_are_enforced(client):
    assert client.get(BASE + "?environment=arbitrary").status_code == 422
    assert client.put(BASE, content="{}", headers={"Content-Type": "text/plain"}).status_code == 415


def test_preferences_do_not_grant_discovery_or_mutate_admin_policy(client, db_session):
    from core.db.models import UserShadow

    db_session.add(UserShadow(user_id="alice", username="Alice", extra_data={"allowed_apps": []}))
    db_session.commit()
    assert (
        client.put(
            BASE, json={"revision": 0, "pinned": ["external:secret"], "order": []}
        ).status_code
        == 200
    )
    directory = client.get("/v1/workbench/catalog").json()["data"]
    assert not any(item["applicationKey"] == "external:secret" for item in directory["items"])
    assert directory["policy"]["revision"] == 0
    assert client.post("/v1/workbench/applications/external:secret/launch").status_code == 404
