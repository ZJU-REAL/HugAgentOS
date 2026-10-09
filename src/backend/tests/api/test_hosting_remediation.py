"""External review regressions at the HTTP and MCP boundaries."""
import asyncio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from tests.api.test_application_hosting import hosted, create, data

@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 422, 429, 500])
def test_site_errors_have_cors(status):
    from api.middleware.cors import setup_cors
    app = FastAPI()
    setup_cors(app)
    @app.get("/site/a/__api/probe")
    def fail():
        raise HTTPException(status, "Invalid submission")
    with TestClient(app) as client:
        response = client.get("/site/a/__api/probe", headers={"Origin": "null"})
        assert response.status_code == status
        assert response.headers["access-control-allow-origin"] == "*"

@pytest.mark.asyncio
async def test_stateless_mcp_get_rejected(monkeypatch):
    from core.services import application_mcp
    monkeypatch.setattr(application_mcp, "authorized_application",
                        lambda *args: (None, {"title": "probe", "tools": []}))
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application_mcp.ApplicationMCPGateway()), base_url="http://probe") as client:
        response = await asyncio.wait_for(client.get("/applications-mcp/" + "a"*32, headers={"Authorization": "Bearer probe", "Accept": "text/event-stream"}), 1)
        assert response.status_code == 405
        assert "POST" in response.headers["allow"]

def test_owner_delete_and_complete_json_export(hosted):
    import csv, io, json
    client, _ = hosted
    app_id = create(client)
    root = f"/v1/applications/{app_id}"
    data(client.post(root + "/tables", json={"name": "entries", "columns": [{"name": "meta", "type": "json"}]}))
    for count in (100, 1):
        data(client.post(root + "/tables/entries/records", json={"rows": [{"meta": {"tier": "gold"}} for _ in range(count)]}))
    response = client.get(root + "/tables/entries/export")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 101
    assert json.loads(rows[0]["meta"]) == {"tier": "gold"}
    record = rows[0]
    path = root + "/tables/entries/records/" + record["id"]
    assert client.delete(path + "?version=9").status_code == 409
    assert client.delete(path + "?version=1").status_code == 200
    assert client.delete(root + "/tables/entries").status_code == 200
    assert client.delete(root).status_code == 200
    assert client.get(root).status_code == 404

def test_public_replacement_history_can_be_restored(hosted):
    from core.services.application_collections import read_collection, replace_collection
    from core.services.application_data import ApplicationDataService
    from core.services.application_schema import TableDefinition, RecordBatch
    client, engine = hosted
    app_id = create(client)
    service = ApplicationDataService(engine)
    service.define_table(app_id, "owner", TableDefinition(name="tasks", public_read=True, public_replace=True, columns=[{"name": "title"}]))
    service.insert(app_id, "owner", "tasks", RecordBatch(rows=[{"title": "Keep me"}]))
    original = read_collection(service, app_id, "owner", "tasks")
    emptied = replace_collection(service, app_id, "owner", "tasks", original["revision"], [])
    root = f"/v1/applications/{app_id}/tables/tasks"
    history = data(client.get(root + "/history"))["items"]
    assert len(history) == 1
    response = client.post(root + "/history/" + history[0]["id"] + "/restore", json={"revision": emptied["revision"]})
    assert response.status_code == 200
    assert data(client.get(root + "/records"))["items"][0]["title"] == "Keep me"

def test_anonymous_cannot_evict_recovery_baseline(hosted):
    from core.services.application_collections import read_collection, replace_collection
    from core.services.application_data import ApplicationDataService
    from core.services.application_schema import TableDefinition, RecordBatch
    from core.services.application_history import list_history, restore
    client, engine = hosted
    app_id = create(client)
    service = ApplicationDataService(engine)
    service.define_table(app_id, "owner", TableDefinition(name="tasks", public_read=True, public_replace=True, columns=[{"name":"title"}]))
    service.insert(app_id, "owner", "tasks", RecordBatch(rows=[{"title": "Original"}]))
    current = read_collection(service, app_id, "owner", "tasks")
    for index in range(22):
        current = replace_collection(service, app_id, "owner", "tasks", current["revision"], [{"title": str(index)}])
    history = list_history(service, app_id, "owner", "tasks")["items"]
    assert len(history) == 20
    restored = restore(service, app_id, "owner", "tasks", history[-1]["id"], current["revision"])
    assert restored["items"][0]["title"] == "Original"

@pytest.mark.asyncio
async def test_mcp_limit_schema_has_bounds():
    from core.services.application_mcp import query_tool
    from core.services.application_schema import ToolDefinition, TableDefinition
    from mcp.server.fastmcp import FastMCP
    app = {"tables": {"entries": TableDefinition(name="entries", columns=[{"name": "title"}]).model_dump()}}
    tool = ToolDefinition(name="entries", description="Read entries", table="entries", fields=["title"])
    mcp = FastMCP("probe")
    mcp.add_tool(query_tool(None, app, tool))
    schema = (await mcp.list_tools())[0].inputSchema["properties"]["limit"]
    assert schema["maximum"] == 100 and schema["minimum"] == 1
