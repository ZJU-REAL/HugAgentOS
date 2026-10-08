"""MCP source projects through the owner API, with disposable independent stores."""

import pytest
from core.auth.backend import get_current_user
from core.db.engine import Base, get_db
from core.db.models import UserShadow
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.api.test_application_hosting import create, data, hosted, table


@pytest.fixture
def projects(hosted, tmp_path, monkeypatch):
    client, _ = hosted
    from api.routes.v1.projects import router

    client.app.include_router(router)
    from api.routes.v1.application_transports import internal_router
    from api.routes.v1.chats.sessions import router as chats_router

    client.app.include_router(chats_router, prefix="/v1/chats")
    client.app.include_router(internal_router)
    monkeypatch.setattr("api.routes.v1.internal_site_auth._check_internal_token", lambda _: None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'projects.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner", email="owner@example.com"))
        db.commit()

    def dependency():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = dependency
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr("core.storage.factory._storage_instance", None)
    yield client
    engine.dispose()


def test_mcp_creation_and_editor_reuse_one_project(projects):
    client = projects
    app = data(client.post("/v1/applications", json={"title": "查询 MCP", "kind": "mcp"}))
    assert app["project_id"]
    first = data(client.post(f"/v1/applications/{app['id']}/editor"))
    second = data(client.post(f"/v1/applications/{app['id']}/editor"))
    assert first["project_id"] == second["project_id"] == app["project_id"]
    assert first["definition"]["app_id"] == app["id"]
    assert first["definition"]["version"] == 0
    assert "token" not in str(first)
    assert data(client.get(f"/v1/applications/{app['id']}"))["project_id"] == app["project_id"]


def test_project_definition_publishes_same_service_and_rejects_stale_source(projects):
    client = projects
    app = data(client.post("/v1/applications", json={"title": "查询 MCP", "kind": "mcp"}))
    table(client, app["id"])
    editor = data(client.post(f"/v1/applications/{app['id']}/editor"))
    import json

    definition = {
        **editor["definition"],
        "tools": [
            {
                "name": "find_entries",
                "description": "Find records",
                "table": "entries",
                "fields": ["name"],
                "filters": ["name"],
            }
        ],
    }
    path = f"/v1/projects/{editor['project_id']}/source"
    data(
        client.put(
            path,
            json={
                "path": "mcp.json",
                "content": json.dumps(definition),
                "revision": editor["revision"],
            },
        )
    )
    first = data(client.post(f"/v1/applications/{app['id']}/mcp/project"))
    assert first["version"] == 1 and first["project_synced"]
    latest = data(client.post(f"/v1/applications/{app['id']}/editor"))
    assert latest["definition"]["version"] == 1
    definition["tools"][0]["description"] = "Updated description"
    data(
        client.put(
            path,
            json={
                "path": "mcp.json",
                "content": json.dumps(definition),
                "revision": latest["revision"],
            },
        )
    )
    assert client.post(f"/v1/applications/{app['id']}/mcp/project").status_code == 409
    definition["version"] = 1
    current = data(client.get(path, params={"path": "mcp.json"}))
    data(
        client.put(
            path,
            json={
                "path": "mcp.json",
                "content": json.dumps(definition),
                "revision": current["revision"],
            },
        )
    )
    second = data(client.post(f"/v1/applications/{app['id']}/mcp/project"))
    assert second["url"] == first["url"] and second["version"] == 2
    assert (
        data(client.get(f"/v1/applications/{app['id']}"))["tools"][0]["description"]
        == "Updated description"
    )


def test_editor_provisions_legacy_mcp_and_enforces_owner_access(projects):
    from types import SimpleNamespace

    client = projects
    app_id = create(client)
    assert data(client.get(f"/v1/applications/{app_id}"))["project_id"] is None
    editor = data(client.post(f"/v1/applications/{app_id}/editor"))
    assert editor["project_id"]
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="other")
    assert client.post(f"/v1/applications/{app_id}/editor").status_code == 404
    assert client.post(f"/v1/applications/{app_id}/mcp/project").status_code == 404


def test_invalid_project_definition_stays_editable_and_cannot_publish(projects):
    client = projects
    app = data(client.post("/v1/applications", json={"title": "查询 MCP", "kind": "mcp"}))
    editor = data(client.post(f"/v1/applications/{app['id']}/editor"))
    data(
        client.put(
            f"/v1/projects/{editor['project_id']}/source",
            json={
                "path": "mcp.json",
                "content": "{broken",
                "revision": editor["revision"],
            },
        )
    )
    assert data(client.post(f"/v1/applications/{app['id']}/editor"))["content"] == "{broken"
    assert client.post(f"/v1/applications/{app['id']}/mcp/project").status_code == 422
    assert data(client.get(f"/v1/applications/{app['id']}"))["mcp_version"] == 0


def test_conversation_creation_is_bound_to_mcp_project(projects):
    client = projects
    response = client.post("/v1/chats", json={"title": "Create query MCP"})
    assert response.status_code == 201
    chat_id = response.json()["data"]["chat_id"]
    app = data(
        client.post(
            "/v1/internal/applications/operation",
            json={
                "user_id": "owner",
                "chat_id": chat_id,
                "action": "create",
                "payload": {"title": "查询 MCP", "kind": "mcp"},
            },
        )
    )
    editor = data(client.post(f"/v1/applications/{app['id']}/editor"))
    assert editor["chat_id"] == chat_id
    chat = data(client.get(f"/v1/chats/{chat_id}"))
    assert chat["project_id"] == editor["project_id"]
    assert chat["metadata"]["site_chat"] is True


def test_management_publication_does_not_overwrite_unpublished_project_draft(projects):
    import json

    client = projects
    app = data(client.post("/v1/applications", json={"title": "查询 MCP", "kind": "mcp"}))
    table(client, app["id"])
    editor = data(client.post(f"/v1/applications/{app['id']}/editor"))
    draft = {
        **editor["definition"],
        "tools": [
            {
                "name": "draft_query",
                "description": "Unpublished draft",
                "table": "entries",
                "fields": ["name"],
                "filters": [],
            }
        ],
    }
    data(
        client.put(
            f"/v1/projects/{editor['project_id']}/source",
            json={
                "path": "mcp.json",
                "content": json.dumps(draft),
                "revision": editor["revision"],
            },
        )
    )
    result = client.post(
        f"/v1/applications/{app['id']}/mcp",
        json={
            "tools": [
                {
                    "name": "another_query",
                    "description": "Management change",
                    "table": "entries",
                    "fields": ["name"],
                    "filters": [],
                }
            ]
        },
    )
    assert result.status_code == 409
    preserved = data(client.post(f"/v1/applications/{app['id']}/editor"))
    assert preserved["definition"] == draft
    assert preserved["published_definition"]["version"] == 0
