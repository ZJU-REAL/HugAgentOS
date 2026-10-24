"""Account connection declarations survive export, sync and local installation."""
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.routes.v1.plugins import router
from core.auth.backend import UserContext, get_current_user
from features.desktop.capabilities import device_catalog, plugins
from core.db.engine import get_db
from features.desktop.capability import entities as entities
from tests.features.desktop.capabilities.test_agents_plugins import cloud_db  # noqa: F401
from tests.db_support import share_session


@pytest.mark.parametrize("connection", ["yida", "lark", "dingtalk", "email"])
def test_exported_plugin_retains_connection(monkeypatch, connection):
    monkeypatch.setattr(entities, "_user_plugins", lambda uid: [{
        "install_id": "demo@owner", "slug": "demo", "name": "Demo",
        "connection": connection, "skills": [], "mcp": [],
    }])
    bundle, _ = entities.resolve_plugin_bundle("owner", "demo@owner")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        definition = json.loads(archive.read("demo/plugin.json"))
    assert definition["connection"] == connection


@pytest.mark.parametrize("connection", ["yida", "lark", "dingtalk", "email"])
def test_device_detail_exposes_connection(index_db, caps_root, monkeypatch, connection):
    monkeypatch.setattr(device_catalog, "_profile", lambda: None)
    monkeypatch.setattr(device_catalog, "_managed_connectors", lambda: [])
    plugins.publish_local_plugin({
        "slug": "account-demo", "name": "Demo", "connection": connection,
        "components": {"skills": [], "mcp": []},
    }, owner_user_id="owner")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: None
    share_session(monkeypatch, None)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="owner", user_center_id="owner", username="owner")
    with TestClient(app) as client:
        response = client.get("/v1/plugins/installed/account-demo/detail")
        assert response.status_code == 200
        assert response.json()["data"]["connection"] == connection


def test_local_import_preserves_connection(index_db, caps_root, tmp_path, monkeypatch):
    from features.desktop.local.plugins import service as service
    from core.db.engine import Base
    Base.metadata.create_all(index_db.kw["bind"])
    monkeypatch.setattr('features.desktop.local.skills.service._invalidate', lambda: None)
    monkeypatch.setattr(device_catalog, "_profile", lambda: None)
    monkeypatch.setattr(device_catalog, "_managed_connectors", lambda: [])
    root = tmp_path / "package"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps({
        "name": "account-demo", "version": "1.0.0",
        "extensions": {"org.hugagent": {"connection": "yida"}},
    }))
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\nDemo")
    installed = service.install("owner", root)
    assert device_catalog.plugin_detail(installed["install_id"], "owner")["connection"] == "yida"

def test_builtin_cloud_bundle_exports_its_manifest_connection(cloud_db):
    from core.db.models import InstalledPlugin
    with cloud_db() as db:
        row = db.query(InstalledPlugin).first()
        row.slug = "yida"
        db.commit()
    bundle, _ = entities.resolve_plugin_bundle("cloud-u", "sites@cloud-u")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        definition = json.loads(archive.read("yida/plugin.json"))
    assert definition["connection"] == "yida"
