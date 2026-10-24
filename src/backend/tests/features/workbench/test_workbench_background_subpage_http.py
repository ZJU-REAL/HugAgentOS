"""Actual HTTP authorization regression over an isolated database and package import.

The existing active resource is seeded as durable metadata. No worker creation or
OS-process behavior is asserted by this module.
"""

import io
import json
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, get_db
from core.db.models import UserShadow
from core.db.models.plugin_resource import PluginResource
from core.capabilities.plugins.resources.crypto import encrypt_secret
from tests.db_support import share_session


@pytest.fixture()
def boundary(monkeypatch):
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "isolated-subpage-test-key")
    from api.routes.v1.plugins import router as plugins
    from features.workbench.api.workbench import router as workbench

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            UserShadow(
                user_id="subpage-owner",
                username="Subpage owner",
                extra_data={"can_import_plugin": True},
            )
        )
        db.commit()
        app = FastAPI()
        app.include_router(workbench)
        app.include_router(plugins)
        app.dependency_overrides[get_db] = lambda: db
        share_session(monkeypatch, db)
        app.dependency_overrides[get_current_user] = lambda: UserContext(
            user_id="subpage-owner",
            user_center_id="subpage-owner",
            username="Subpage owner",
        )
        with TestClient(app) as client:
            yield client, db
    Base.metadata.drop_all(engine)
    engine.dispose()


def package_with_subpage():
    from tests.features.workbench.workbench_plugin_support import package

    original = zipfile.ZipFile(io.BytesIO(package()))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in original.namelist():
            content = original.read(name)
            if name == "plugin.json":
                manifest = json.loads(content)
                ui = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
                ui["applications"][0]["permissions"] = ["background"]
                ui["applications"][0]["views"] = [
                    {
                        "id": "details",
                        "path": "details",
                        "title": "Details",
                        "moduleId": "details",
                    }
                ]
                ui["modules"].append(
                    {
                        "id": "details",
                        "surface": "workspace",
                        "entry": "web/details.html",
                        "title": "Details",
                        "grants": ["background"],
                    }
                )
                content = json.dumps(manifest)
            archive.writestr(name, content)
        archive.writestr("web/details.html", "<h1>Details</h1>")
    return output.getvalue()


def test_subpage_background_uses_owned_server_module_and_rejects_forgery(boundary):
    client, db = boundary
    imported = client.post(
        "/v1/plugins/import",
        files={"file": ("subpage.zip", package_with_subpage(), "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    item = next(
        item
        for item in client.get("/v1/workbench/catalog").json()["data"]["items"]
        if item["declaration"]["target"]["kind"] == "plugin"
    )
    binding = {"revision": str(item["revision"]), **item["binding"]}
    key = item["applicationKey"]
    path = f"/v1/workbench/applications/{key}/instances/details-instance/activity"
    response = client.post(path, json={"binding": binding, "moduleId": "details"})
    assert response.status_code == 200, response.text

    for rid, module_id in [("details-resource", "details"), ("main-resource", "page")]:
        db.add(
            PluginResource(
                resource_id=rid,
                user_id="subpage-owner",
                chat_id=None,
                application_key=key,
                instance_id="details-instance",
                install_id=binding["installId"],
                revision=binding["packageRevision"],
                slug=binding["slug"],
                module_id=module_id,
                scope=binding["executionScope"],
                descriptor="{}",
                status="active",
                request_digest=rid,
                created_at=time.time(),
                expires_at=time.time() + 60,
            )
        )
    db.commit()

    # Registration does not grant background execution.
    assert (
        client.post(path + "/background", json={"resourceId": "details-resource"}).status_code
        == 403
    )
    consent = client.post(path + "/consent", json={"capabilities": ["background"]})
    assert consent.status_code == 200, consent.text

    # The public request accepts only resourceId; module identity is server-owned.
    assert (
        client.post(
            path + "/background",
            json={"resourceId": "main-resource", "moduleId": "details"},
        ).status_code
        == 422
    )
    mismatch = client.post(path + "/background", json={"resourceId": "main-resource"})
    assert mismatch.status_code == 403, mismatch.text
    assert mismatch.json()["detail"]["code"] == "resource_module_mismatch"
    assert client.get("/v1/workbench/me/activity").json()["data"]["background"] == []

    changed_origin = {
        **binding,
        "origin": {**binding["origin"], "id": "forged-origin"},
    }
    assert (
        client.post(path, json={"binding": changed_origin, "moduleId": "details"}).status_code
        == 409
    )

    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other-owner", user_center_id="other-owner", username="Other owner"
    )
    assert (
        client.post(path + "/background", json={"resourceId": "details-resource"}).status_code
        == 404
    )
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="subpage-owner",
        user_center_id="subpage-owner",
        username="Subpage owner",
    )

    # Durable metadata alone cannot authorize a real background process.
    rejected = client.post(path + "/background", json={"resourceId": "details-resource"})
    assert rejected.status_code == 410, rejected.text
    assert rejected.json()["detail"] == "resource_credentials_expired"

    # Even a decryptable record cannot replace a physical deadline contract.
    row = db.get(PluginResource, "details-resource")
    row.descriptor = encrypt_secret("{}")
    db.commit()
    unsupported = client.post(path + "/background", json={"resourceId": "details-resource"})
    assert unsupported.status_code == 403, unsupported.text
    assert unsupported.json()["detail"]["code"] == "unsupported_capability"
    projection = client.get("/v1/workbench/me/activity")
    assert projection.status_code == 200, projection.text
    assert projection.json()["data"]["background"] == []
