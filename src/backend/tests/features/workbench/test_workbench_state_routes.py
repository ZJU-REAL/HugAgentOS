"""Public application state HTTP contract."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from tests.db_support import share_session

BASE = "/v1/workbench/me/application-state/builtin:chat"
SCOPE = "?workspace=main&packageRevision=1"


def body(revision=0, **values):
    return {"revision": revision, "schemaVersion": "1", "payload": {"filter": "open"}, **values}


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


def test_state_durable_cas_and_scope_isolation(client):
    assert client.get(BASE + SCOPE).status_code == 200
    assert client.get(BASE + SCOPE).json()["data"]["revision"] == 0
    assert client.put(BASE + SCOPE, json=body()).status_code == 200
    assert client.put(BASE + SCOPE, json=body()).status_code == 409
    assert client.get(BASE + SCOPE).json()["data"]["payload"] == {"filter": "open"}
    assert client.get(BASE + "?workspace=other&packageRevision=1").json()["data"]["revision"] == 0
    assert client.get(BASE + "?packageRevision=old").status_code == 409
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="bob", user_center_id="bob", username="Bob"
    )
    assert client.get(BASE + SCOPE).json()["data"]["revision"] == 0


def test_schema_change_requires_explicit_reset(client):
    assert client.put(BASE + SCOPE, json=body()).status_code == 200
    result = client.get(BASE + SCOPE + "&schemaVersion=2").json()["data"]
    assert result["resetRequired"] is True and result["payload"] is None and result["revision"] == 1
    assert client.put(BASE + SCOPE, json=body(1, schemaVersion="2")).status_code == 409
    assert client.put(BASE + SCOPE, json=body(1, schemaVersion="2", reset=True)).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "secret"},
        {"nested": [{"password": "secret"}]},
        {"url": "https://example.com/?token=secret"},
        {"x": "Bearer abc"},
        {"x": "a" * (256 * 1024)},
    ],
)
def test_credentials_and_oversize_never_persist_or_echo(client, payload):
    result = client.put(BASE + SCOPE, json=body(payload=payload))
    assert result.status_code in [413, 422]
    assert "secret" not in result.text
    assert client.get(BASE + SCOPE).json()["data"]["revision"] == 0


def test_json_identity_and_hidden_boundaries(client, db_session):
    from core.db.models import ContentBlock

    assert client.put(BASE + SCOPE, json=body(user_id="bob")).status_code == 422
    assert (
        client.put(BASE + SCOPE, content="{}", headers={"Content-Type": "text/plain"}).status_code
        == 415
    )
    assert (
        client.get(
            "/v1/workbench/me/application-state/external:missing?packageRevision=1"
        ).status_code
        == 404
    )
    db_session.add(
        ContentBlock(
            id="workbench_policy",
            payload={"revision": 1, "defaultPinned": [], "forced": [], "hidden": ["builtin:chat"]},
        )
    )
    db_session.commit()
    assert client.get(BASE + SCOPE).status_code == 404
    assert client.put(BASE + SCOPE, json=body()).status_code == 404


def test_user_quota_across_workspaces(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_STATE_USER_BYTES", "256")
    assert client.put(BASE + SCOPE, json=body(payload={"filter": "a" * 180})).status_code == 200
    assert (
        client.put(
            BASE + "?workspace=two&packageRevision=1", json=body(payload={"filter": "b" * 180})
        ).status_code
        == 413
    )
