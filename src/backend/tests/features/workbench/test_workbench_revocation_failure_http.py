"""Real logout/catalog HTTP with persisted credentials and injected shutdown failure."""

import asyncio
import io
import json
import time
import zipfile

import pytest

from core.auth import session
from core.config.settings import settings
from core.db.engine import Base, SessionLocal, engine
from core.db.models import ContentBlock, UserShadow
from core.db.models.plugin_resource import (
    PluginResource,
    PluginResourceAssetTicket,
    PluginResourceTicket,
)
from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.crypto import encrypt_secret
from fastapi.testclient import TestClient

USER = "wb-user"


@pytest.fixture()
def boundary(monkeypatch):
    from tests.features.workbench.test_workbench_external_routes import AUTH
    monkeypatch.setenv("CONFIG_TOKEN", AUTH["Authorization"].removeprefix("Bearer "))
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "wb-revocation-isolated-secret-key")
    from core.sandbox import factory

    class ControlledDeadlineProvider:
        """Transport stub only: these tests make no physical process claim."""
        name = "controlled_deadline"

        async def set_process_deadline(self, process_id, *, sandbox_session_id,
                                       user_id, binding, expires_at, absolute_deadline=None):
            assert process_id in {"worker-a", "worker-b", "cleanup-a", "healthy-b"}
            assert user_id == USER
            assert sandbox_session_id.startswith("workbench-")
            assert len(binding) == 64 and all(c in "0123456789abcdef" for c in binding)
            assert time.time() < expires_at <= time.time() + 91
            assert absolute_deadline is None or expires_at <= absolute_deadline
            return {"armed": True}

    monkeypatch.setattr(factory, "get_sandbox_provider", lambda: ControlledDeadlineProvider())
    from api.app import app

    assert settings.auth.mode == "session"
    assert session._use_memory_store()
    session.process_state().MEMORY_SESSIONS.clear()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(
            UserShadow(
                user_id=USER,
                user_center_id=USER,
                username="Reader",
                extra_data={"can_import_plugin": True},
            )
        )
        db.commit()
    token = asyncio.run(session.create_session({"user_id": USER, "username": "Reader"}))
    client = TestClient(app)
    client.cookies.set(settings.session.cookie_name, token)
    yield client, token
    client.close()
    session.process_state().MEMORY_SESSIONS.clear()
    Base.metadata.drop_all(engine)


def background_package():
    from tests.features.workbench.workbench_plugin_support import package

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package())) as original:
        with zipfile.ZipFile(output, "w") as archive:
            for name in original.namelist():
                value = original.read(name)
                if name == "plugin.json":
                    manifest = json.loads(value)
                    ui = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
                    ui["applications"][0]["permissions"] = ["background"]
                    ui["modules"][0]["grants"] = ["background"]
                    value = json.dumps(manifest)
                archive.writestr(name, value)
    return output.getvalue()


def setup_workers(client):
    from features.workbench import activity

    response = client.post(
        "/v1/plugins/import",
        files={"file": ("workers.zip", background_package(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    item = next(
        value
        for value in client.get("/v1/workbench/catalog").json()["data"]["items"]
        if value["declaration"]["target"]["kind"] == "plugin"
    )
    binding = {"revision": str(item["revision"]), **item["binding"]}
    prefix = f"/v1/workbench/applications/{item['applicationKey']}/instances/"
    tickets = {}
    for rid in ["worker-a", "worker-b"]:
        path = prefix + rid + "/activity"
        assert client.post(path, json={"binding": binding}).status_code == 200
        assert (
            client.post(path + "/consent", json={"capabilities": ["background"]}).status_code == 200
        )
        with SessionLocal() as db:
            row = PluginResource(
                resource_id=rid,
                user_id=USER,
                chat_id=None,
                application_key=item["applicationKey"],
                instance_id=rid,
                install_id=binding["installId"],
                revision=binding["packageRevision"],
                slug=binding["slug"],
                module_id="page",
                scope=binding["executionScope"],
                descriptor=encrypt_secret(json.dumps({"process_id": rid, "provider": "controlled_deadline"})),
                status="active",
                request_digest=rid,
                created_at=time.time(),
                expires_at=time.time() + 60,
            )
            db.add(row)
            db.add(
                ContentBlock(
                    id="workbench_plugin_lease:" + rid,
                    payload={
                        "userId": USER,
                        "applicationKey": item["applicationKey"],
                        "revision": binding["packageRevision"],
                        "expiresAt": time.time() + 60,
                        "closed": False,
                    },
                )
            )
            db.commit()
            tickets[rid] = (
                store.ticket(db, row, "https://host.test"),
                store.asset_ticket(db, row),
            )
    assert (
        client.post(
            prefix + "worker-a/activity/background", json={"resourceId": "worker-a"}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert activity.read(db, USER)["background"]["worker-a"]["expiresAt"] > time.time()
    return item, prefix, tickets


def credentials_revoked():
    from features.workbench import activity
    from features.workbench.revocation import EXTERNAL_PREFIXES

    with SessionLocal() as db:
        value = activity.read(db, USER)
        assert value["grants"] == {}
        assert value["instances"] == {}
        assert value["background"].get("worker-a", {}).get("expiresAt", 0) == 0
        assert db.query(PluginResourceTicket).count() == 0
        assert db.query(PluginResourceAssetTicket).count() == 0
        for row in db.query(ContentBlock).all():
            assert not row.id.startswith(EXTERNAL_PREFIXES)
            if row.id.startswith("workbench_plugin_lease:"):
                assert row.payload["closed"] is True
        for row in db.query(PluginResource).all():
            assert row.status in {"closing", "closed"}
            assert row.expires_at == 0


@pytest.mark.parametrize("trigger", ["logout", "unavailable"])
def test_revocation_commits_before_failed_close_and_continues(boundary, monkeypatch, trigger):
    from features.workbench import activity
    from tests.features.workbench.test_workbench_auth_routes import configure, launch, redeem
    from tests.features.workbench.test_workbench_external_routes import APP, AUTH, BASE, create

    client, old_token = boundary
    item, prefix, tickets = setup_workers(client)
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    exchange = launch(client)
    calls = []
    fail_first = [True]

    async def shutdown(row, path, body=None, **kwargs):
        assert path == "/shutdown"
        # Independent connection proves the whole credential barrier committed.
        calls.append(row.resource_id)
        credentials_revoked()
        if row.resource_id == "worker-a" and fail_first[0]:
            raise RuntimeError("injected provider shutdown failure")
        return {"closed": True}

    monkeypatch.setattr(service, "request", shutdown)
    if trigger == "logout":
        response = client.post("/v1/auth/logout", json={})
        assert response.status_code == 200, response.text
        assert asyncio.run(session.validate_session(old_token)) is None
        client.cookies.set(settings.session.cookie_name, old_token)
        assert client.get("/v1/workbench/catalog").status_code == 401
        fresh = asyncio.run(session.create_session({"user_id": USER, "username": "Reader"}))
        client.cookies.set(settings.session.cookie_name, fresh)
    else:
        assert (
            client.patch(
                "/v1/plugins/installed/" + item["binding"]["installId"] + "/enable",
                json={"enabled": False},
            ).status_code
            == 200
        )
        assert (
            client.put(
                BASE + "/admin/external-applications/external:wb_reports",
                headers=AUTH,
                json={"revision": 1, "declaration": APP, "enabled": False},
            ).status_code
            == 200
        )
        from features.workbench.revocation_sweep import reconcile_owner
        asyncio.run(reconcile_owner(USER))

    assert calls == ["worker-a", "worker-b"]
    credentials_revoked()
    assert redeem(client, exchange).status_code == 401
    for rid, (ticket, asset_ticket) in tickets.items():
        with SessionLocal() as db:
            assert store.consume(db, ticket, rid, "https://host.test") is None
            row = db.get(PluginResource, rid)
            assert row.status == ("closing" if rid == "worker-a" else "closed")
        assert (
            client.get(
                f"/v1/plugin-resource-assets/cloud/{asset_ticket}/web/index.html"
            ).status_code
            == 404
        )
        assert (
            client.post(prefix + rid + "/resources/" + rid + "/lease", json={}).status_code == 410
        )
        assert (
            client.post(
                prefix + rid + "/resources/" + rid + "/command",
                json={"id": "after-revocation", "action": "state", "params": {}},
            ).status_code
            == 410
        )
    assert client.get("/v1/workbench/me/activity").json()["data"]["background"] == []
    # A subsequent real janitor sweep can retry the retained exact failed handle.
    fail_first[0] = False
    from core.capabilities.plugins.resources import lifecycle

    with SessionLocal() as db:
        asyncio.run(lifecycle.reconcile(USER))
        db.expire_all()
        assert db.get(PluginResource, "worker-a").status == "closed"
    assert calls[-1] == "worker-a"
