"""Agent-facing schema and errors must explain valid application definitions."""

import asyncio

from tests.api.test_application_hosting import hosted


def test_mcp_exposes_typed_application_payload():
    from mcp_servers.site_publish_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    tool = next(item for item in tools if item.name == "manage_application")
    definitions = tool.inputSchema.get("$defs", {})
    assert definitions["ColumnDefinition"]["properties"]["type"]["enum"] == [
        "text",
        "integer",
        "number",
        "boolean",
        "date",
        "json",
    ]
    assert "TableDefinition" in definitions
    assert "IndustryImport" not in definitions
    assert "import_industry" not in tool.inputSchema["properties"]["action"]["enum"]
    assert "app_id" in tool.inputSchema["properties"]


def test_internal_validation_returns_field_errors_without_values(hosted, monkeypatch):
    from api.routes.v1 import application_transports
    from core.db.engine import get_db

    client, _ = hosted
    client.app.include_router(application_transports.internal_router)
    client.app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr("api.routes.v1.internal_site_auth._check_internal_token", lambda _: None)
    response = client.post(
        "/v1/internal/applications/operation",
        json={
            "user_id": "owner",
            "action": "table",
            "app_id": "missing",
            "payload": {
                "name": "entries",
                "columns": [{"name": "email", "type": "string"}],
                "app_id": "private-value-not-for-errors",
            },
        },
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(item["loc"] == ["columns", 0, "type"] for item in errors)
    assert any(item["loc"] == ["app_id"] for item in errors)
    assert "private-value-not-for-errors" not in response.text
    assert all("input" not in item for item in errors)
