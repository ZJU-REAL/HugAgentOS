import time
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.db.engine import get_db
from core.db.models.plugin_resource import PluginResource, PluginResourceAssetTicket
from core.plugins.resources import store

async def test_opaque_iframe_assets_use_revocable_ticket_without_session_cookie(monkeypatch, tmp_path):
    from api.routes.v1 import plugin_resources
    from core.auth import session
    from core.config.settings import settings
    from core.plugins.resources import service, installation
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    token = await session.create_session({"user_id": "owner", "user_center_id": "owner", "username": "test"})
    engine = create_engine("sqlite://")
    for model in (PluginResource, PluginResourceAssetTicket):
        model.__table__.create(engine)
    db = Session(engine)
    row = PluginResource(resource_id="resource", user_id="owner", chat_id="chat",
        install_id="install", revision="revision", slug="fixture", module_id="module",
        scope="cloud", descriptor="", status="active", created_at=time.time(), expires_at=time.time()+100)
    db.add(row); db.commit()
    context = {"headers": {"cookie": settings.session.cookie_name+"="+token}, "path": "/asset-ticket", "method": "POST"}
    ticket = store.asset_ticket(db, row, context)
    web = tmp_path / "web"; web.mkdir()
    (web / "index.html").write_text('<script src="main.js"></script>')
    (web / "main.js").write_text('window.loaded = true;')
    monkeypatch.setattr(installation, "resolve", lambda *args: SimpleNamespace(package=tmp_path))
    monkeypatch.setattr(service, "authorized", lambda database, resource, owner: store.get(database, resource, owner))
    app = FastAPI()
    app.include_router(plugin_resources.router)
    app.dependency_overrides[get_db] = lambda: db
    prefix = "/v1/plugin-resource-assets/cloud/" + ticket
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for asset in ("index.html", "main.js"):
                response = await client.get(prefix+"/"+asset, headers={"Origin": "null"})
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert "connect-src 'none'" in response.headers["content-security-policy"]
            assert (await client.get(prefix.replace("/cloud/", "/local/")+"/main.js")).status_code == 409
            assert (await client.get(prefix.replace(ticket, ticket+"invalid")+"/main.js")).status_code == 404
            assert (await client.get(prefix+"/../outside.txt")).status_code != 200
            await session.revoke_session(token)
            assert (await client.get(prefix+"/main.js")).status_code == 401
    finally:
        db.close()
        await session.revoke_session(token)


def test_asset_capability_urls_are_redacted_from_application_logs():
    from api.middleware.logging import sanitize_log
    assert sanitize_log("/v1/plugin-resource-assets/cloud/private-ticket/browser/main.js") == (
        "/v1/plugin-resource-assets/cloud/[redacted]/browser/main.js")


def test_asset_auth_migration_preserves_and_invalidates_existing_tickets():
    import importlib.util
    from pathlib import Path
    from sqlalchemy import inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    filename = Path(__file__).parents[1] / "alembic/versions/browserassets02_revocable_asset_tickets.py"
    spec = importlib.util.spec_from_file_location("asset_migration", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plugin_resource_asset_tickets (digest TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plugin_resource_asset_tickets VALUES ('old-ticket')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT auth_enc FROM plugin_resource_asset_tickets")).scalar() == ""
            migration.downgrade()
            assert "auth_enc" not in {c["name"] for c in inspect(connection).get_columns("plugin_resource_asset_tickets")}
            assert connection.execute(text("SELECT digest FROM plugin_resource_asset_tickets")).scalar() == "old-ticket"


def test_existing_sqlite_asset_tickets_gain_a_safe_authentication_default():
    from sqlalchemy import MetaData, text
    from core.db.schema_reconcile import reconcile_metadata_schema
    metadata = MetaData()
    table = PluginResourceAssetTicket.__table__.to_metadata(metadata)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plugin_resource_asset_tickets (digest VARCHAR(64) PRIMARY KEY, resource_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, expires_at FLOAT NOT NULL)"))
        connection.execute(text("INSERT INTO plugin_resource_asset_tickets VALUES ('legacy','resource','owner',9999999999)"))
    reconcile_metadata_schema(engine, metadata)
    with Session(engine) as db:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            store.resolve_asset_ticket(db, "invalid")
        assert error.value.status_code == 404
        assert db.get(PluginResourceAssetTicket, "legacy").auth_enc == ""
