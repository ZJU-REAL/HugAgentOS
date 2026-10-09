"""Conversation publication installs one private, usable MCP without exposing secrets."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.api.test_application_hosting import create, data, hosted, table


@pytest.fixture
def personal(hosted, tmp_path, monkeypatch):
    from api.routes.v1 import application_transports
    from core.db.engine import get_db
    from core.db.models import AdminMcpServer

    client, app_engine = hosted
    platform = create_engine(
        "sqlite:///" + str(tmp_path / "personal-platform.db"),
        connect_args={"check_same_thread": False},
    )
    from core.db.engine import Base
    from core.db.models import UserShadow

    Base.metadata.create_all(platform)
    factory = sessionmaker(platform)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner", email="owner@example.com"))
        db.commit()

    def db_dependency():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = db_dependency
    client.app.include_router(application_transports.internal_router)
    client.app.include_router(application_transports.mcp_router)
    monkeypatch.setattr("api.routes.v1.internal_site_auth._check_internal_token", lambda _: None)
    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *a: {"can_add_mcp": True}
    )

    # The real protocol adapter is exercised in-process instead of opening a test port.
    async def probe(row, db=None, **kwargs):
        from core.services.mcp_management_service import decrypt_mcp_headers

        headers = decrypt_mcp_headers(row.headers)
        headers.update({"Accept": "application/json, text/event-stream"})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            headers=headers,
        ) as transport:
            init = await transport.post(
                row.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            if init.status_code != 200:
                return False, "protocol initialize failed"
            tools = await transport.post(
                row.url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            row.tools_json = tools.json()["result"]["tools"]
        return True, ""

    monkeypatch.setattr("core.services.mcp_management_service.probe_mcp_connectivity", probe)
    monkeypatch.setattr("core.services.mcp_management_service.refresh_mcp_caches", lambda: None)
    yield client, factory
    platform.dispose()


def publish(client, app_id, **changes):
    return client.post(
        "/v1/internal/applications/publish-mcp",
        json={
            "user_id": "owner",
            "app_id": app_id,
            "tools": [
                {
                    "name": "find_entries",
                    "description": "Find records",
                    "table": "entries",
                    "fields": ["name"],
                    "filters": ["name"],
                }
            ],
            **changes,
        },
    )


def test_conversation_publishes_private_mcp_and_updates_same_entry(personal):
    from core.db.models import AdminMcpServer
    from core.services.mcp_management_service import decrypt_mcp_headers

    client, factory = personal
    app_id = create(client)
    table(client, app_id)
    first = data(publish(client, app_id))
    assert first["installed"] is True
    assert "token" not in first and "headers" not in first
    with factory() as db:
        row = db.get(AdminMcpServer, first["server_id"])
        assert row.owner_user_id == "owner" and row.is_enabled
        assert row.headers["Authorization"].startswith("enc:v1:")
        old_auth = decrypt_mcp_headers(row.headers)
        assert row.tools_json[0]["name"] == "find_entries"
    second = data(publish(client, app_id))
    assert second["server_id"] == first["server_id"]
    assert second["version"] == first["version"] + 1
    assert client.post(first["url"], headers=old_auth, json={}).status_code == 401
    with factory() as db:
        assert db.query(AdminMcpServer).count() == 1
        auth = decrypt_mcp_headers(db.get(AdminMcpServer, first["server_id"]).headers)
    response = client.post(
        first["url"],
        headers={**auth, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_entries", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert client.delete(f"/v1/applications/{app_id}/mcp").status_code == 200
    with factory() as db:
        row = db.get(AdminMcpServer, first["server_id"])
        assert not row.is_enabled and not row.headers
    assert client.post(first["url"], headers=auth, json={}).status_code == 401


def test_permission_and_owner_rejected_before_publication(personal, monkeypatch):
    from core.db.models import AdminMcpServer

    client, factory = personal
    app_id = create(client)
    table(client, app_id)
    monkeypatch.setattr("core.auth.capabilities.resolve_user_capabilities", lambda *a: {})
    assert publish(client, app_id).status_code == 403
    assert data(client.get(f"/v1/applications/{app_id}"))["mcp_enabled"] is False
    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *a: {"can_add_mcp": True}
    )
    assert publish(client, app_id, user_id="other").status_code == 404
    assert data(client.get(f"/v1/applications/{app_id}"))["mcp_version"] == 0
    with factory() as db:
        assert db.query(AdminMcpServer).count() == 0


def test_failed_probe_leaves_disabled_connection_and_retry_repairs(personal, monkeypatch):
    from core.db.models import AdminMcpServer
    from core.services import mcp_management_service as management

    client, factory = personal
    app_id = create(client)
    table(client, app_id)
    probe = management.probe_mcp_connectivity

    async def fail(*args, **kwargs):
        return False, "private-diagnostic-secret"

    monkeypatch.setattr(management, "probe_mcp_connectivity", fail)
    failed = data(publish(client, app_id))
    assert failed["installed"] is False and failed["connection_verified"] is False
    assert "private-diagnostic-secret" not in str(failed) and "token" not in failed
    with factory() as db:
        row = db.get(AdminMcpServer, failed["server_id"])
        assert not row.is_enabled
    monkeypatch.setattr(management, "probe_mcp_connectivity", probe)
    repaired = data(publish(client, app_id))
    assert repaired["installed"] is True and repaired["server_id"] == failed["server_id"]


def test_manual_updates_and_rollback_refresh_personal_credentials(personal):
    from core.db.models import AdminMcpServer
    from core.services.mcp_management_service import decrypt_mcp_headers

    client, factory = personal
    app_id = create(client)
    table(client, app_id)
    receipt = data(publish(client, app_id))
    body = {
        "tools": [
            {
                "name": "read_names",
                "description": "Read names",
                "table": "entries",
                "fields": ["name"],
                "filters": [],
            }
        ]
    }
    updated = data(client.post(f"/v1/applications/{app_id}/mcp", json=body))
    with factory() as db:
        row = db.get(AdminMcpServer, receipt["server_id"])
        assert row.is_enabled and row.tools_json[0]["name"] == "read_names"
        assert decrypt_mcp_headers(row.headers)["Authorization"] == "Bearer " + updated["token"]
    restored = data(client.post(f"/v1/applications/{app_id}/mcp/rollback", json={"version": 1}))
    with factory() as db:
        row = db.get(AdminMcpServer, receipt["server_id"])
        assert row.is_enabled and row.tools_json[0]["name"] == "find_entries"
        assert decrypt_mcp_headers(row.headers)["Authorization"] == "Bearer " + restored["token"]


def test_publish_tool_has_typed_tools_and_requires_injected_identity(monkeypatch):
    from mcp_servers.site_publish_mcp.server import mcp, publish_mcp

    tool = next(item for item in asyncio.run(mcp.list_tools()) if item.name == "publish_mcp")
    assert set(tool.inputSchema["required"]) == {"app_id", "tools"}
    assert "user_id" not in tool.inputSchema["properties"]
    assert "ToolDefinition" in tool.inputSchema["$defs"]
    assert asyncio.run(publish_mcp("a" * 32, [])) == {"error": "Current user identity is required"}


def test_publish_tool_forwards_only_injected_identity_and_typed_tools(monkeypatch):
    from core.services.application_schema import ToolDefinition
    from mcp_servers.site_publish_mcp import server

    calls = []
    monkeypatch.setattr(server, "_identity", lambda _: {"user_id": "owner", "chat_id": "chat"})

    async def call(path, payload, *, timeout):
        calls.append((path, payload, timeout))
        return {"installed": True, "connection_verified": True}

    monkeypatch.setattr(server.impl, "_call_backend", call)
    tool = ToolDefinition(name="find", description="Find", table="names", fields=["name"])
    result = asyncio.run(server.publish_mcp("a" * 32, [tool]))
    assert result["installed"] is True
    assert calls == [
        (
            "/v1/internal/applications/publish-mcp",
            {
                "user_id": "owner",
                "chat_id": "chat",
                "app_id": "a" * 32,
                "tools": [tool.model_dump()],
            },
            60.0,
        )
    ]


def test_legacy_conversation_publish_uses_personal_flow_without_token(personal):
    client, _ = personal
    app_id = create(client)
    table(client, app_id)
    response = client.post(
        "/v1/internal/applications/operation",
        json={
            "user_id": "owner",
            "action": "publish_mcp",
            "app_id": app_id,
            "payload": {
                "tools": [
                    {
                        "name": "find_entries",
                        "description": "Find",
                        "table": "entries",
                        "fields": ["name"],
                    }
                ]
            },
        },
    )
    receipt = data(response)
    assert receipt["installed"] is True and receipt["connection_verified"] is True
    assert "token" not in receipt


def test_registry_failure_is_partial_and_does_not_expose_secrets(personal, monkeypatch):
    from core.services import mcp_management_service as management

    client, _ = personal
    app_id = create(client)
    table(client, app_id)
    encrypt = management.encrypt_mcp_headers

    def failure(headers):
        raise RuntimeError("secret-value-must-not-escape")

    monkeypatch.setattr(management, "encrypt_mcp_headers", failure)
    receipt = data(publish(client, app_id))
    assert receipt["published"] is True and receipt["installed"] is False
    assert "token" not in receipt and "secret-value-must-not-escape" not in str(receipt)
    monkeypatch.setattr(management, "encrypt_mcp_headers", encrypt)
    assert data(publish(client, app_id))["installed"] is True


def test_concurrent_publications_keep_one_connection_with_latest_credentials(personal):
    from concurrent.futures import ThreadPoolExecutor

    from core.db.models import AdminMcpServer
    from core.services.mcp_management_service import decrypt_mcp_headers

    client, factory = personal
    app_id = create(client)
    table(client, app_id)
    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(lambda _: data(publish(client, app_id)), range(2)))
    assert {receipt["version"] for receipt in receipts} == {1, 2}
    assert all(receipt["installed"] for receipt in receipts)
    assert len({receipt["server_id"] for receipt in receipts}) == 1
    with factory() as db:
        assert db.query(AdminMcpServer).count() == 1
        row = db.get(AdminMcpServer, receipts[0]["server_id"])
        assert row.extra_config["hosted_mcp_version"] == 2 and row.is_enabled
        auth = decrypt_mcp_headers(row.headers)
    result = client.post(
        receipts[0]["url"],
        headers={**auth, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert result.status_code == 200
    assert result.json()["result"]["tools"][0]["name"] == "find_entries"
