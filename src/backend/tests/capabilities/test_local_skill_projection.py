"""Actual upload/edit/disable/delete routes must publish the desktop installation."""

import io
import zipfile
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.capabilities import registry, skills, store
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import AdminSkill
from api.routes.v1 import me_capabilities, desktop_capabilities, admin_skills


@pytest.fixture
def local_client(index_db, caps_root, tmp_path, monkeypatch):
    with index_db() as db:
        AdminSkill.__table__.create(db.get_bind(), checkfirst=True)
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "test-config-only")
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "workspace" / "skills"))
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr("core.services.desktop_cloud_bridge.get_state", lambda: None)
    monkeypatch.setattr(me_capabilities, "_require_flag", lambda *args: None)
    monkeypatch.setattr(
        "core.services.skill_management_service.ensure_ontology_build_valid",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("core.config.catalog_loader.load_catalog", lambda **kwargs: {})
    app = FastAPI()
    for router in (me_capabilities.router, desktop_capabilities.router, admin_skills.router):
        app.include_router(router)

    def database():
        with index_db() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="owner", user_center_id="c", username="Owner"
    )
    app.dependency_overrides[admin_skills.require_admin] = lambda: None
    yield TestClient(app)
    from core.agent_skills.loader import get_skill_loader

    get_skill_loader(reset=True)


def test_upload_edit_disable_delete_reaches_real_installation_list(local_client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: uploaded-local\ndescription: uploaded fixture\n---\nInitial content",
        )
        archive.writestr("scripts/value.txt", "v1")
    response = local_client.post(
        "/v1/me/skills/upload", files={"file": ("skill.zip", buffer.getvalue(), "application/zip")}
    )
    assert response.status_code == 201, response.text
    iid = "skill:local:uploaded-local"
    first = registry.get(iid)
    assert first is not None and first.ready and first.payload["from_db"]
    listing = local_client.get("/v1/desktop/capabilities/installations?kind=skill")
    assert listing.status_code == 200, listing.text
    assert iid in {item["install_id"] for item in listing.json()["data"]["items"]}
    old = store.get("skill", "local", "uploaded-local", first.resolved_revision)
    edit = local_client.put(
        "/v1/me/skills/uploaded-local/files/scripts/value.txt", json={"content": "v2"}
    )
    assert edit.status_code == 200, edit.text
    updated = registry.get(iid)
    assert updated.resolved_revision != first.resolved_revision
    assert (old.path / "scripts/value.txt").read_text() == "v1"
    assert (
        store.get("skill", "local", "uploaded-local", updated.resolved_revision).path
        / "scripts/value.txt"
    ).read_text() == "v2"
    toggle = local_client.put("/v1/admin/skills/uploaded-local/toggle", json={"is_enabled": False})
    assert toggle.status_code == 200, toggle.text
    assert not registry.get(iid).enabled
    assert "uploaded-local" not in skills.resolve_for_user("owner").chosen
    deleted = local_client.delete("/v1/me/skills/uploaded-local")
    assert deleted.status_code == 200, deleted.text
    assert registry.get(iid).state == "removed"
    assert "uploaded-local" not in skills.resolve_for_user("owner").chosen
    assert old.path.is_dir()
