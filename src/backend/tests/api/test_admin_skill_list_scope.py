"""The admin skill list shows global skills only, like the MCP admin list.

The loader loads every user's private skill (it must resolve them by id at
runtime), so the list has to drop them itself, together with plugin skills.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.v1 import admin_skills
from core.capabilities.skills.backends import CompositeBackend, DatabaseBackend, FilesystemBackend
from core.capabilities.skills.loader import MultiSourceSkillLoader
from core.db.models import AdminSkill


@pytest.fixture
def skill_api(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AdminSkill.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(
        DatabaseBackend, "_get_session_and_model", lambda self: (sessions, AdminSkill)
    )

    for sid in ("builtin-on", "builtin-off"):
        folder = tmp_path / sid
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {sid}\ndescription: fixture\n---\nInstructions", encoding="utf-8"
        )
    loader = MultiSourceSkillLoader(
        CompositeBackend([DatabaseBackend(), FilesystemBackend(tmp_path, "built-in")])
    )

    monkeypatch.setattr(admin_skills, "get_skill_loader", lambda: loader)
    monkeypatch.setattr(admin_skills, "_refresh_caches", lambda: None)
    monkeypatch.setattr(admin_skills, "get_skill_icons", lambda db: {})
    monkeypatch.setattr(admin_skills, "get_enabled_ids", lambda kind: ["builtin-on"])
    monkeypatch.setattr(admin_skills, "get_catalog", lambda **kwargs: {"skills": []})

    app = FastAPI()
    app.include_router(admin_skills.router)
    app.dependency_overrides[admin_skills.require_admin] = lambda: None

    def get_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[admin_skills.get_db] = get_db
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def _install(sessions, sid, *, owner=None, enabled=True, status="ready", plugin=None):
    with sessions() as db:
        db.add(
            AdminSkill(
                skill_id=sid,
                display_name=sid,
                description="fixture",
                skill_content=f"---\nname: {sid}\ndescription: fixture\n---\nInstructions",
                owner_user_id=owner,
                is_enabled=enabled,
                dep_status=status,
                source_plugin=plugin,
            )
        )
        db.commit()


def _listed(client):
    response = client.get("/v1/admin/skills")
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["data"]}


def test_list_shows_global_skills_and_hides_private_and_plugin_skills(skill_api):
    client, sessions = skill_api
    _install(sessions, "global-on")
    _install(sessions, "global-off", enabled=False)
    _install(sessions, "global-pending", enabled=False, status="pending")
    _install(sessions, "private-ready", owner="user-a")
    _install(sessions, "private-pending", owner="user-a", status="pending")
    _install(sessions, "plugin-skill", plugin="fixture-plugin")
    _install(sessions, "plugin-private", owner="user-a", plugin="fixture-plugin")

    listed = _listed(client)

    assert set(listed) == {
        "builtin-on",
        "builtin-off",
        "global-on",
        "global-off",
        "global-pending",
    }
    assert listed["builtin-on"]["is_enabled"] is True
    assert listed["builtin-off"]["is_enabled"] is False
    assert listed["global-on"]["is_enabled"] is True
    assert listed["global-off"]["is_enabled"] is False


def test_private_skill_is_still_loaded_for_runtime(skill_api):
    """Hiding it from the admin list must not drop it from the loader."""
    client, sessions = skill_api
    _install(sessions, "private-ready", owner="user-a")

    assert "private-ready" not in _listed(client)
    assert "private-ready" in admin_skills.get_skill_loader().load_all_metadata()


@pytest.mark.parametrize("enabled", [True, False])
def test_global_toggle_survives_list_reload(skill_api, enabled):
    client, sessions = skill_api
    _install(sessions, "global-skill", enabled=not enabled)

    response = client.put("/v1/admin/skills/global-skill/toggle", json={"is_enabled": enabled})
    assert response.status_code == 200

    assert _listed(client)["global-skill"]["is_enabled"] is enabled
