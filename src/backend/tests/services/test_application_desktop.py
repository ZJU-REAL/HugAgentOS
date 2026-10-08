"""Desktop startup and authenticated cloud forwarding contracts."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text


def test_local_startup_provisions_separate_store(tmp_path, monkeypatch):
    from core.auth import desktop_bridge  # noqa: F401 - import before replacing settings
    from core.config import application_hosting
    from core.config import settings as config
    from core.db import engine as platform
    from core.services import application_store as store

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            deploy=SimpleNamespace(is_local=True),
            storage=SimpleNamespace(root=tmp_path),
            db=SimpleNamespace(pool_timeout=5),
        ),
    )
    monkeypatch.setattr(
        application_hosting, "application_hosting_settings", SimpleNamespace(database_url="")
    )
    monkeypatch.setattr(platform, "DATABASE_URL", "sqlite:///" + str(tmp_path / "platform.sqlite"))
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: False)
    store.application_engine.cache_clear()
    try:
        store.initialize_local_store()
        engine = store.application_engine()
        store.require_store(engine)
        assert engine.url.database == str(tmp_path / "applications.sqlite")
        with engine.begin() as connection:
            connection.execute(
                store.applications.insert(),
                dict(
                    id="persist",
                    user_id="owner",
                    title="Persist",
                    tables={},
                    tools=[],
                    mcp_enabled=False,
                    created_at=__import__("datetime").datetime.now(),
                ),
            )
        store.initialize_local_store()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT title FROM hosted_applications")) == "Persist"
    finally:
        if store.application_engine.cache_info().currsize:
            store.application_engine().dispose()
        store.application_engine.cache_clear()


def test_cloud_startup_does_not_provision(monkeypatch):
    from core.auth import desktop_bridge  # noqa: F401 - import before replacing settings
    from core.config import settings as config
    from core.services import application_store as store

    monkeypatch.setattr(config, "settings", SimpleNamespace(deploy=SimpleNamespace(is_local=False)))
    monkeypatch.setattr(
        store, "application_engine", lambda: pytest.fail("cloud provisioning forbidden")
    )
    store.initialize_local_store()


@pytest.mark.asyncio
async def test_dual_callback_forwards_without_local_write(monkeypatch):
    from api.routes.v1 import application_transports as routes
    from core.services import desktop_cloud_bridge, desktop_site_publish
    from core.services.application_schema import InternalOperation

    monkeypatch.setattr(desktop_cloud_bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(
        "api.routes.v1.internal_site_auth._check_internal_token", lambda value: None
    )
    calls = []

    async def forward(tool, arguments, **identity):
        calls.append((tool, arguments, identity))
        return {"ok": True, "id": "cloud-app"}

    monkeypatch.setattr(desktop_site_publish, "forward_local_site_tool", forward)
    monkeypatch.setattr(
        routes, "perform_operation", lambda *args: pytest.fail("local write forbidden")
    )
    body = InternalOperation(
        user_id="owner",
        chat_id="local-chat",
        action="create",
        payload={"title": "Form", "site_id": "cloud-site"},
    )
    result = await routes.internal_operation(body, x_internal_token="test", db=None)
    assert result["data"]["id"] == "cloud-app"
    assert calls == [
        (
            "manage_application",
            {"action": "create", "payload": body.payload, "app_id": ""},
            {"user_id": "owner", "chat_id": "local-chat"},
        )
    ]


def test_local_retirement_is_idempotent_and_verifies_archive(tmp_path):
    from core.db.site_storage_retirement import retire_site_storage

    engine = create_engine("sqlite:///" + str(tmp_path / "old.sqlite"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE site_kv (site_id TEXT, k TEXT, v TEXT)")
        connection.exec_driver_sql("INSERT INTO site_kv VALUES ('old', 'name', '中文')")
    directory = tmp_path / "archives"
    assert retire_site_storage(engine, directory) is None
    assert inspect(engine).has_table("site_kv")
    from core.db.site_storage_retirement import _digest

    receipt = directory / "kv-to-sql-test"
    receipt.mkdir(parents=True)
    payload = {"site_kv": [{"site_id": "old", "k": "name", "v": "中文"}]}
    (receipt / "receipt.json").write_text(
        json.dumps(
            {"verified": True, "legacy_sha256": _digest(payload), "sites": [{"site_id": "old"}]}
        )
    )
    archive = retire_site_storage(engine, directory)
    assert not inspect(engine).has_table("site_kv")
    assert json.loads(archive.read_text(encoding="utf-8"))["tables"]["site_kv"][0]["v"] == "中文"
    assert retire_site_storage(engine, tmp_path / "archives") is None
    assert len(list((tmp_path / "archives").glob("*.json"))) == 1


def test_pending_local_migration_preserves_rows(tmp_path):
    from core.db.site_storage_retirement import retire_site_storage

    engine = create_engine("sqlite:///" + str(tmp_path / "old.sqlite"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE site_kv (v TEXT)")
        connection.exec_driver_sql("INSERT INTO site_kv VALUES ('keep')")
    blocked = tmp_path / "not-directory"
    blocked.write_text("blocked")
    assert retire_site_storage(engine, blocked) is None
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT v FROM site_kv")) == "keep"


def test_local_conversation_mcp_receipt_uses_cloud_origin():
    from core.services.desktop_site_publish import localize_application_result

    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"url": "/applications-mcp/app", "token": "one-time-test", "version": 1}
                ),
            }
        ]
    }
    localize_application_result(result, "https://cloud.example.test/")
    receipt = json.loads(result["content"][0]["text"])
    assert receipt["url"] == "https://cloud.example.test/applications-mcp/app"
    assert receipt["token"] == "one-time-test"


def test_real_local_bootstrap_retains_unmigrated_and_provisions(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    source = Path(__file__).parents[2]
    platform = tmp_path / "platform.sqlite"
    engine = create_engine("sqlite:///" + str(platform))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE site_kv (v TEXT)")
        connection.exec_driver_sql("INSERT INTO site_kv VALUES ('本机存量')")
    engine.dispose()
    code = """
import asyncio
from sqlalchemy import inspect
from api.app import _startup_ensure_tables
from core.db.engine import engine
from core.services.application_data import ApplicationDataService
from core.services.application_store import application_engine
from core.services.application_schema import ApplicationDefinition, TableDefinition, RecordBatch
asyncio.run(_startup_ensure_tables())
assert inspect(engine).has_table("site_kv")
service = ApplicationDataService()
app = service.create("owner", ApplicationDefinition(title="本机表单"))
service.define_table(app["id"], "owner", TableDefinition(
    name="entries", columns=[{"name":"name", "required":True}]))
service.insert(app["id"], "owner", "entries", RecordBatch(rows=[{"name":"本机记录"}]))
asyncio.run(_startup_ensure_tables())
assert service.query(app["id"], "owner", "entries")["items"][0]["name"] == "本机记录"
assert application_engine().url.database != engine.url.database
print("LOCAL_BOOTSTRAP_PASSED")
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(source),
        "DEPLOY_PROFILE": "local",
        "DATABASE_URL": "sqlite:///" + str(platform),
        "STORAGE_PATH": str(tmp_path / "storage"),
        "JX_EDITION": "ce",
        "JX_LICENSE_REQUIRED": "false",
        "APPLICATION_DATABASE_URL": "",
        "HUGAGENT_DESKTOP_BRIDGE_SECRET": "",
        "ENV": "dev",
    }
    env.pop("SITE_STORAGE_BACKUP_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=40
    )
    assert result.returncode == 0, result.stderr[-5000:]
    assert "LOCAL_BOOTSTRAP_PASSED" in result.stdout
    archives = list((tmp_path / "storage/migration-backups").glob("*.json"))
    assert archives == []
    with create_engine("sqlite:///" + str(platform)).connect() as connection:
        assert connection.scalar(text("SELECT v FROM site_kv")) == "本机存量"


@pytest.mark.asyncio
async def test_cloud_gateway_mcp_receipt_is_localized(monkeypatch):
    import httpx
    import mcp.types
    from core.llm.gateway_mcp_tool import GatewayMCPTool
    from core.services import desktop_cloud_bridge as bridge
    from tests.capabilities.test_runtime_recovery import state

    monkeypatch.setattr("core.capabilities.paths.capabilities_enabled", lambda: True)
    st = state("cloud-user")
    monkeypatch.setattr(bridge, "get_state", lambda: st)

    def cloud(request):
        assert request.headers["authorization"] == "Bearer " + st["token"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"url": "/applications-mcp/app", "token": "test"}),
                        }
                    ]
                }
            },
        )

    tool = GatewayMCPTool(
        mcp_name="sites-site_publish",
        source_plugin="sites",
        tool=mcp.types.Tool(name="manage_application", inputSchema={"type": "object"}),
        invoke_url=st["cloud_base"] + "/api/v1/desktop/capability/gateway/sites-site_publish/call",
        schema_hash="a" * 64,
        timeout=10,
        headers={"Authorization": "Bearer " + st["token"]},
        transport=httpx.MockTransport(cloud),
    )
    result = await tool(action="publish_mcp", app_id="app", payload={"tools": []})
    assert json.loads(result.content[0].text)["url"] == "https://cloud.example/applications-mcp/app"


@pytest.mark.asyncio
async def test_personal_mcp_publication_forwards_cloud_identity_and_tools(monkeypatch):
    from api.routes.v1 import application_transports as routes
    from core.services import desktop_cloud_bridge, desktop_site_publish
    from core.services.application_schema import InternalMCPPublish
    from core.services.desktop_gateway_uploads import result_localizer

    monkeypatch.setattr(desktop_cloud_bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr("api.routes.v1.internal_site_auth._check_internal_token", lambda _: None)
    calls = []

    async def forward(tool, arguments, **identity):
        calls.append((tool, arguments, identity))
        return {
            "installed": True,
            "connection_verified": True,
            "url": "/applications-mcp/" + "a" * 32,
        }

    monkeypatch.setattr(desktop_site_publish, "forward_local_site_tool", forward)
    body = InternalMCPPublish(
        user_id="local-owner",
        chat_id="chat",
        app_id="a" * 32,
        tools=[{"name": "find", "description": "Find names", "table": "names", "fields": ["name"]}],
    )
    result = await routes.internal_publish_mcp(body, x_internal_token="test", db=None)
    assert result["data"]["installed"] is True
    assert calls == [
        (
            "publish_mcp",
            body.model_dump(exclude={"user_id", "chat_id"}),
            {"user_id": "local-owner", "chat_id": "chat"},
        )
    ]
    reply = {"content": [{"type": "text", "text": json.dumps(result["data"])}]}
    result_localizer("sites", "publish_mcp")(reply, "https://cloud.example")
    assert json.loads(reply["content"][0]["text"])["url"].startswith(
        "https://cloud.example/applications-mcp/"
    )
