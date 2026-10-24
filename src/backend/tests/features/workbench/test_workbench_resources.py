"""Public resource lifecycle seam with isolated SQLite and a fake runtime boundary."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.db.models.plugin_resource import (
    PluginResource,
    PluginResourceAssetTicket,
    PluginResourceTicket,
)
from features.hosted_apps.resources import admission as application
from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.crypto import encrypt_secret
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "isolated-wb11-test-key")


@pytest.fixture
def resource_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Shared owner/catalog/retained-revision checks now require their real durable tables.
    from core.db import engine as db_engine
    from core.db.engine import Base
    from sqlalchemy.orm import sessionmaker

    Base.metadata.create_all(engine)
    # Closing a worker opens sessions of its own (core.db.threads.run_in_session).
    monkeypatch.setattr(db_engine, "SessionLocal", sessionmaker(bind=engine))
    with Session(engine) as db:
        yield db
    engine.dispose()


def record(**changes):
    values = dict(
        resource_id="r",
        user_id="owner",
        chat_id=None,
        application_key="plugin:key",
        instance_id="instance",
        install_id="install",
        revision="revision",
        slug="fixture",
        module_id="page",
        scope="cloud",
        descriptor=encrypt_secret("{}"),
        status="active",
        request_digest="digest",
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    return PluginResource(**{**values, **changes})


def authorize(monkeypatch):
    # Import the exact resolver before patching legacy resolution to avoid cached fake imports.
    from features.workbench import catalog, installation_binding

    monkeypatch.setattr(
        catalog,
        "readable",
        lambda db, user, key: {
            "availability": {"available": True},
            "binding": {"installId": "install", "packageRevision": "revision", "slug": "fixture"},
            "declaration": {"target": {"kind": "plugin", "moduleId": "page"}},
        },
    )
    monkeypatch.setattr(
        service.installation,
        "resolve",
        lambda *args: SimpleNamespace(revision="revision", install_id="install", slug="fixture"),
    )
    monkeypatch.setattr(installation_binding, "resolve", lambda *args: service.installation.resolve(*args))
    from core.capabilities.plugins.resources import owners

    monkeypatch.setattr(owners, "execution_scope", lambda: "cloud")


@pytest.mark.parametrize(
    "changes",
    [
        {"chat_id": "chat"},
        {"application_key": None},
        {"instance_id": None},
        {"instance_id": ""},
        {"application_key": ""},
    ],
)
def test_database_rejects_ambiguous_or_absent_owner(resource_db, changes):
    resource_db.add(record(**changes))
    with pytest.raises(IntegrityError):
        resource_db.commit()


def test_cross_user_instance_install_revision_and_scope_rejected(resource_db, monkeypatch):
    authorize(monkeypatch)
    row = record()
    resource_db.add(row)
    resource_db.commit()
    assert application.owned(resource_db, "owner", "plugin:key", "instance", "r") == row
    for user, key, instance in [
        ("other", "plugin:key", "instance"),
        ("owner", "plugin:other", "instance"),
        ("owner", "plugin:key", "other"),
    ]:
        with pytest.raises(HTTPException):
            application.owned(resource_db, user, key, instance, "r")
    for name, value in [("install_id", "other"), ("revision", "old"), ("scope", "local")]:
        old = getattr(row, name)
        setattr(row, name, value)
        with pytest.raises(HTTPException):
            service.authorized(resource_db, "r", "owner")
        setattr(row, name, old)


async def test_close_is_idempotent_revokes_tickets_and_prevents_lease(resource_db, monkeypatch):
    authorize(monkeypatch)
    row = record()
    resource_db.add(row)
    resource_db.commit()
    ticket = store.ticket(resource_db, row, "https://host.test")
    await service.close(row)
    await service.close(row)
    resource_db.refresh(row)
    assert row.status == "closed"
    assert store.consume(resource_db, ticket, "r", "https://host.test") is None
    with pytest.raises(HTTPException) as error:
        await application.renew("owner", "plugin:key", "instance", "r")
    assert error.value.status_code == 410


async def test_janitor_cleans_expired_application_and_continues_after_failure(
    resource_db, monkeypatch
):
    from core.capabilities.plugins.resources import lifecycle

    first, second = record(resource_id="a", expires_at=0), record(resource_id="b", expires_at=0)
    resource_db.add_all([first, second])
    resource_db.commit()
    close = service.close

    async def sometimes(row):
        if row.resource_id == "a":
            raise RuntimeError("injected cleanup failure")
        await close(row)

    monkeypatch.setattr(service, "close", sometimes)
    await lifecycle.reconcile()
    resource_db.expire_all()
    assert second.status == "closed"
    assert first.status == "active"


def test_desktop_owner_upgrade_preserves_chat_and_is_reentrant():
    from core.db.resource_owner_upgrade import reconcile_resource_owners
    from sqlalchemy import inspect, text

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE plugin_resources (resource_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO plugin_resources VALUES ('old','chat')"))
    reconcile_resource_owners(engine)
    reconcile_resource_owners(engine)
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT chat_id FROM plugin_resources WHERE resource_id='old'")
            ).scalar()
            == "chat"
        )
        connection.execute(
            text(
                "INSERT INTO plugin_resources (resource_id,application_key,instance_id) VALUES ('app','plugin:key','instance')"
            )
        )
        assert inspect(connection).get_columns("plugin_resources")[1]["nullable"]


async def test_open_retries_share_worker_and_closed_request_never_replays(resource_db, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from core.sandbox import factory
    import core.sandbox.processes.interactive as interactive

    authorize(monkeypatch)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"closed":false}')

        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    selected = SimpleNamespace(
        revision="revision",
        install_id="install",
        slug="fixture",
        ui={
            "contributes": {
                "modules": [
                    {
                        "id": "page",
                        "resource": {"entry": "runtime", "callable": "worker:run"},
                        "grants": ["resource.attach", "resource.command"],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(service.installation, "resolve", lambda *args: selected)
    launch = AsyncMock(
        return_value={
            "url": f"http://127.0.0.1:{server.server_port}",
            "headers": {"X-Test": "isolated"},
            "process_id": "worker",
            "provider": "test",
        }
    )
    monkeypatch.setattr(interactive, "launch", launch)
    monkeypatch.setattr(factory, "get_sandbox_provider", lambda: SimpleNamespace(name="test"))
    args = (
        "owner",
        "plugin:key",
        "instance",
        "install",
        "revision",
        "cloud",
        "page",
        "request",
    )
    try:
        first = await application.create(*args)
        second = await application.create(*args)
        assert first["resource_id"] == second["resource_id"]
        assert first["chat_id"] is None
        assert launch.await_count == 1
        assert launch.await_args.args[4].startswith("workbench-")
        row = application.owned(
            resource_db, "owner", "plugin:key", "instance", first["resource_id"]
        )
        await service.close(row)
        with pytest.raises(HTTPException) as error:
            await application.create(*args)
        assert error.value.status_code == 410
        assert launch.await_count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_revocation_blocks_reads_but_owner_can_still_release(resource_db, monkeypatch):
    authorize(monkeypatch)
    row = record()
    resource_db.add(row)
    resource_db.commit()
    from features.workbench import catalog

    def revoked(*args):
        raise HTTPException(404, "app_not_found")

    monkeypatch.setattr(catalog, "readable", revoked)
    with pytest.raises(HTTPException):
        service.authorized(resource_db, "r", "owner")
    closing = application.owned(
        resource_db, "owner", "plugin:key", "instance", "r", allow_closed=True
    )
    await service.close(closing)
    resource_db.refresh(closing)
    assert closing.status == "closed"


async def test_authenticated_http_lease_close_and_instance_isolation(resource_db, monkeypatch):
    import httpx
    from api import deps  # Compose the host before testing its feature routes.
    from features.workbench.api.workbench import require_json_write
    from features.workbench.api.workbench_resources import routes
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    authorize(monkeypatch)
    row = record()
    resource_db.add(row)
    resource_db.commit()
    app = FastAPI()
    app.include_router(routes(APIRoute, require_json_write), prefix="/v1/workbench")
    app.dependency_overrides[get_db] = lambda: resource_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="owner", user_center_id="owner", username="Owner", email=""
    )
    from core.capabilities.plugins.resources import owners
    monkeypatch.setattr(owners, "resolve_installation", lambda *args: SimpleNamespace(
        ui={"contributes": {"modules": [{"id": "page", "grants": ["resource.command"]}]}}
    ))
    dispatched = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(service, "command", dispatched)
    prefix = "/v1/workbench/applications/plugin:key/instances/instance/resources"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(prefix + "/r/lease", json={})).status_code == 200
        command = {"id": "cmd", "action": "refresh", "params": {}}
        assert (await client.post(prefix + "/r/command", json=command)).status_code == 200
        dispatched.assert_awaited_once()
        assert (await client.post(prefix.replace("/instance/", "/other/") + "/r/command",
                                  json=command)).status_code == 404
        assert (
            await client.post(prefix.replace("/instance/", "/other/") + "/r/lease", json={})
        ).status_code == 404
        assert (await client.post(prefix + "/r/lease", content="")).status_code == 415
        assert (
            await client.delete(prefix + "/r", headers={"Content-Type": "application/json"})
        ).status_code == 200
        assert (
            await client.delete(prefix + "/r", headers={"Content-Type": "application/json"})
        ).status_code == 200
        assert (await client.post(prefix + "/r/lease", json={})).status_code == 410


def test_server_migration_is_reentrant_and_refuses_lossy_downgrade():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).parents[3] / "alembic/versions/workbenchresources01_application_owner.py"
    spec = importlib.util.spec_from_file_location("wb11_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE plugin_resources (resource_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO plugin_resources VALUES ('old','chat')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            connection.execute(
                text(
                    "INSERT INTO plugin_resources (resource_id,application_key,instance_id) VALUES ('app','plugin:key','instance')"
                )
            )
            with pytest.raises(RuntimeError, match="Export application resource"):
                migration.downgrade()
            connection.execute(text("DELETE FROM plugin_resources WHERE resource_id='app'"))
            migration.downgrade()
            assert {c["name"] for c in inspect(connection).get_columns("plugin_resources")} == {
                "resource_id",
                "chat_id",
            }
            assert (
                connection.execute(text("SELECT chat_id FROM plugin_resources")).scalar() == "chat"
            )
