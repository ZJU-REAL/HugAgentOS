"""Resource reconciliation must isolate cleanup retries from authoritative app revocation.

Resources are persisted fixtures; provider shutdown is fault injected. This is
authorization/lifecycle boundary evidence, not real OS-worker acceptance.
"""

import asyncio
import json
import time

from fastapi.testclient import TestClient
from features.workbench import activity
from features.workbench.revocation_sweep import reconcile_owner
from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from core.db.models.plugin_resource import PluginResource
from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.crypto import encrypt_secret
from tests.features.workbench.test_workbench_revocation_failure_http import boundary, background_package, USER  # noqa: F401

INSTANCE_A = "closing-instance-0001"
INSTANCE_B = "healthy-instance-0002"


def setup(client):
    imported = client.post(
        "/v1/plugins/import",
        files={"file": ("cleanup-isolation.zip", background_package(), "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    item = next(
        item
        for item in client.get("/v1/workbench/catalog").json()["data"]["items"]
        if item["declaration"]["target"]["kind"] == "plugin"
    )
    key = item["applicationKey"]
    prefix = "/v1/workbench/applications/" + key
    binding = {"revision": str(item["revision"]), **item["binding"]}
    tickets = {}
    for rid, instance in [("cleanup-a", INSTANCE_A), ("healthy-b", INSTANCE_B)]:
        context = prefix + "/instances/" + instance + "/activity"
        assert (
            client.post(context, json={"binding": binding, "moduleId": "page"}).status_code == 200
        )
        assert (
            client.post(context + "/consent", json={"capabilities": ["background"]}).status_code
            == 200
        )
        lease = client.post(
            prefix + "/revision-leases", json={"revision": item["revision"], "instanceId": instance}
        )
        assert lease.status_code == 200, lease.text
        with SessionLocal() as db:
            row = PluginResource(
                resource_id=rid,
                user_id=USER,
                chat_id=None,
                application_key=key,
                instance_id=instance,
                install_id=binding["installId"],
                revision=binding["packageRevision"],
                slug=binding["slug"],
                module_id="page",
                scope=binding["executionScope"],
                descriptor=encrypt_secret(
                    json.dumps({"process_id": rid, "provider": "controlled_deadline"})
                ),
                status="active",
                request_digest=rid,
                created_at=time.time(),
                expires_at=time.time() + 60,
            )
            db.add(row)
            db.commit()
            tickets[rid] = store.ticket(db, row, "https://host.test")
    promoted = client.post(
        prefix + "/instances/" + INSTANCE_B + "/activity/background",
        json={"resourceId": "healthy-b"},
    )
    assert promoted.status_code == 200, promoted.text
    return item, prefix, tickets


def test_failed_closing_resource_does_not_revoke_healthy_sibling(boundary, monkeypatch):
    client, _ = boundary
    item, prefix, tickets = setup(client)
    calls = []
    fail = [True]

    async def shutdown(row, path, body=None, **kwargs):
        assert path == "/shutdown"
        calls.append(row.resource_id)
        if row.resource_id == "cleanup-a" and fail[0]:
            raise RuntimeError("isolated shutdown fault")
        return {"closed": True}

    monkeypatch.setattr(service, "request", shutdown)
    # A normal close fails. Its own access is ended, but application permission is valid.
    closer = TestClient(client.app, raise_server_exceptions=False)
    closer.cookies.update(client.cookies)
    try:
        response = closer.delete(
            prefix + "/instances/" + INSTANCE_A + "/resources/cleanup-a",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 500
    finally:
        closer.close()
    with SessionLocal() as db:
        assert db.get(PluginResource, "cleanup-a").status == "closing"
    # Reading the directory must leave cleanup to the resource reconciler.
    assert client.get("/v1/workbench/catalog/events").status_code == 200
    assert calls == ["cleanup-a"]
    asyncio.run(reconcile_owner(USER))
    assert calls == ["cleanup-a", "cleanup-a"]
    with SessionLocal() as db:
        value = activity.read(db, USER)
        assert item["applicationKey"] in value["grants"]
        assert INSTANCE_B in value["instances"]
        assert value["background"]["healthy-b"]["expiresAt"] > time.time()
        assert db.get(PluginResource, "healthy-b").status == "active"
        assert store.consume(db, tickets["healthy-b"], "healthy-b", "https://host.test") is not None
        for row in db.query(ContentBlock).all():
            if row.id.startswith("workbench_plugin_lease:"):
                assert row.payload["closed"] is False
    renew = client.put(
        prefix + "/revision-leases", json={"revision": item["revision"], "instanceId": INSTANCE_B}
    )
    assert renew.status_code == 200, renew.text
    alive = client.post(prefix + "/instances/" + INSTANCE_B + "/resources/healthy-b/lease", json={})
    assert alive.status_code == 200, alive.text
    assert client.get(prefix).status_code == 200

    fail[0] = False
    asyncio.run(reconcile_owner(USER))
    assert calls == ["cleanup-a", "cleanup-a", "cleanup-a"]
    with SessionLocal() as db:
        assert db.get(PluginResource, "cleanup-a").status == "closed"
        assert db.get(PluginResource, "healthy-b").status == "active"

    # Actual authorization loss still revokes every instance before cleanup.
    disabled = client.patch(
        "/v1/plugins/installed/" + item["binding"]["installId"] + "/enable", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    asyncio.run(reconcile_owner(USER))
    with SessionLocal() as db:
        value = activity.read(db, USER)
        assert value["grants"] == {}
        assert value["instances"] == {}
        assert db.get(PluginResource, "healthy-b").status == "closed"
        for row in db.query(ContentBlock).all():
            if row.id.startswith("workbench_plugin_lease:"):
                assert row.payload["closed"] is True
