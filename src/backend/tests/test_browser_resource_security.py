"""Security invariants independent of real account credentials."""
import importlib.util
import time
from pathlib import Path
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from core.db.models.plugin_resource import PluginResource, PluginResourceTicket, PluginResourceAssetTicket
from core.plugins.resources import store
from core.llm import mcp_invocation as proof

def test_invocation_proof_rejects_impersonation_expiry_and_wrong_audience(monkeypatch):
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "isolated-test-signing-key")
    headers = {**proof.issue("browser_runtime", "owner", "chat"), "x-current-user-id": "owner", "x-chat-id": "chat"}
    assert proof.verify(headers, "browser_runtime")["user"] == "owner"
    for overrides, audience in [
        ({"x-current-user-id": "other"}, "browser_runtime"),
        ({"x-chat-id": "other-chat"}, "browser_runtime"),
        ({proof.HEADER: headers[proof.HEADER] + "0"}, "browser_runtime"),
        ({}, "another-tool"),
    ]:
        with pytest.raises(ValueError, match="not_authorized"):
            proof.verify({**headers, **overrides}, audience)
    now = time.time()
    monkeypatch.setattr(proof.time, "time", lambda: now + 100)
    with pytest.raises(ValueError, match="not_authorized"):
        proof.verify(headers, "browser_runtime")
    assert proof.for_url("https://third-party.example/mcp", "owner", "chat") == {}

def test_tickets_are_origin_bound_single_use_and_asset_owner_bound():
    engine = create_engine("sqlite://")
    for model in (PluginResource, PluginResourceTicket, PluginResourceAssetTicket):
        model.__table__.create(engine)
    with Session(engine) as db:
        row = PluginResource(resource_id="resource", user_id="owner", chat_id="chat",
            install_id="install", revision="revision", slug="fixture", module_id="module",
            scope="local", descriptor="", status="active", created_at=time.time(), expires_at=time.time()+100)
        db.add(row); db.commit()
        token = store.ticket(db, row, "http://localhost:12345")
        assert store.consume(db, token, "resource", "https://attacker.example") is None
        assert store.consume(db, token, "another-resource", "http://localhost:12345") is None
        assert store.consume(db, token, "resource", "http://localhost:12345") == "owner"
        assert store.consume(db, token, "resource", "http://localhost:12345") is None
        asset = store.asset_ticket(db, row)
        assert store.resolve_asset_ticket(db, asset) == ("resource", "owner", {})
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            store.resolve_asset_ticket(db, asset + "invalid")

def test_migration_upgrade_and_downgrade_match_resource_tables():
    filename = Path(__file__).parents[1] / "alembic/versions/browserresources01_plugin_resources.py"
    spec = importlib.util.spec_from_file_location("browser_migration", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            tables = inspect(connection).get_table_names()
            assert len(tables) == 5
            assert "origin" in {c["name"] for c in inspect(connection).get_columns("plugin_resource_tickets")}
            migration.downgrade()
            assert not inspect(connection).get_table_names()

async def test_viewer_authentication_is_revoked_with_real_session_store(monkeypatch):
    from core.auth import session
    from core.config.settings import settings
    from core.plugins.resources import authentication
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    token = await session.create_session({"user_id": "owner", "user_center_id": "owner", "username": "test"})
    context = {"headers": {"cookie": settings.session.cookie_name + "=" + token}, "path": "/api/v1/plugin-resources/resource/attach", "method": "POST"}
    await authentication.validate(context, "owner", None)
    await session.revoke_session(token)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await authentication.validate(context, "owner", None)
    assert error.value.status_code == 401

async def test_expired_starting_reservation_is_cleaned_without_endpoint(monkeypatch):
    from core.plugins.resources import lifecycle
    engine = create_engine("sqlite://")
    for model in (PluginResource, PluginResourceTicket, PluginResourceAssetTicket):
        model.__table__.create(engine)
    monkeypatch.setattr(store, "descriptor", lambda row: {})
    with Session(engine) as db:
        row = PluginResource(resource_id="starting", user_id="owner", chat_id="chat",
            install_id="install", revision="revision", slug="fixture", module_id="module",
            scope="local", descriptor="", status="starting", created_at=0, expires_at=0)
        db.add(row); db.commit()
        await lifecycle.reconcile(db, "owner")
        assert db.get(PluginResource, "starting").status == "closed"


async def test_opensandbox_endpoint_auth_is_confined_to_control_plane_origin():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.sandbox.interactive import endpoint
    connection = SimpleNamespace(
        get_base_url=lambda: "http://sandbox.test:8080",
        get_api_key=lambda: "gateway-secret",
        headers={"X-Gateway-Route": "route"},
    )
    sandbox = SimpleNamespace(connection_config=connection,
        get_endpoint=AsyncMock(return_value=SimpleNamespace(
            endpoint="sandbox.test:8080/sandboxes/id/proxy/1234", headers={})))
    provider = SimpleNamespace(name="opensandbox", _service_loop=None,
        _get_or_create_session=AsyncMock(return_value=SimpleNamespace(sandbox=sandbox)))
    url, headers = await endpoint(provider, "chat", "owner", 1234)
    assert url.startswith("http://sandbox.test:8080/")
    assert headers["OPEN-SANDBOX-API-KEY"] == "gateway-secret"
    assert headers["X-Gateway-Route"] == "route"
    sandbox.get_endpoint.return_value = SimpleNamespace(
        endpoint="external.test:1234", headers={"X-Endpoint-Route": "public"})
    _, headers = await endpoint(provider, "chat", "owner", 1234)
    assert headers == {"X-Endpoint-Route": "public"}


def test_opensandbox_template_allows_the_actual_builtin_skill_mount(monkeypatch, tmp_path):
    import tomllib
    from pathlib import Path
    import importlib
    importlib.import_module("core.sandbox._opensandbox_internals")
    from core.sandbox import _opensandbox_volumes as volumes
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HOST_REPO_PATH", str(root))
    monkeypatch.setattr(volumes, "_BUILTIN_SKILLS_HOST_PATH", None)
    source = Path(volumes._resolve_builtin_skills_host_path())
    rendered = (root / "docker/opensandbox-config.toml.tpl").read_text().replace(
        "@@HOST_REPO_PATH@@", str(root)).replace("@@HOST_STORAGE_PATH@@", str(tmp_path))
    allowed = tomllib.loads(rendered)["storage"]["allowed_host_paths"]
    assert any(source == Path(path) or source.is_relative_to(path) for path in allowed)


def test_invocation_proof_binds_exact_plugin_installation(monkeypatch):
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "isolated-test-signing-key")
    plugin = "plugin:local:browser-user-owned"
    headers = {**proof.issue("browser_runtime", "owner", "chat", plugin),
        proof.PLUGIN_HEADER: plugin, "x-current-user-id": "owner", "x-chat-id": "chat"}
    assert proof.verify(headers, "browser_runtime")["plugin"] == plugin
    headers[proof.PLUGIN_HEADER] = "plugin:local:different-source"
    with pytest.raises(ValueError, match="not_authorized"):
        proof.verify(headers, "browser_runtime")


@pytest.mark.parametrize("changed_body", [True, False])
async def test_resource_callback_rejects_changed_signed_installation(monkeypatch, changed_body):
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import HTTPException
    from api.routes.v1 import plugin_resources
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "isolated-test-signing-key")
    source = "plugin:local:installation-a"
    headers = {**proof.issue("browser_runtime", "owner", "chat", source),
        proof.PLUGIN_HEADER: source, "x-current-user-id": "owner", "x-chat-id": "chat",
        "x-internal-token": "isolated-test-signing-key", "x-hugagent-mcp-audience": "browser_runtime"}
    request = Request({"type":"http", "headers":[(k.lower().encode(), v.encode()) for k,v in headers.items()]})
    body = plugin_resources.ToolRequest(chat_id="chat", slug="fixture", module_id="browser",
        install_id="plugin:local:installation-b" if changed_body else source,
        resource_id="resource-b", action="state")
    monkeypatch.setattr(plugin_resources.service, "authorized", lambda *args: SimpleNamespace(
        chat_id="chat", slug="fixture", module_id="browser", install_id="plugin:local:installation-b"))
    with pytest.raises(HTTPException) as error:
        await plugin_resources.tool_resource(body, request, db=object())
    assert error.value.status_code == 403
    assert error.value.detail in {"plugin_binding_mismatch", "resource_binding_mismatch"}
