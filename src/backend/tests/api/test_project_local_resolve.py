"""POST /v1/projects/local/resolve: shell directory -> local project (CLI contract §8.2)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.v1.projects as ee_routes
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import Project
from tests.services.project_instructions_support import env  # noqa: F401

_CE_ROUTES = Path(__file__).resolve().parents[4] / "ce/overlay/src/backend/api/routes/v1/projects.py"


def _load_ce_routes():
    spec = importlib.util.spec_from_file_location("ce_overlay_projects_routes", _CE_ROUTES)
    module = importlib.util.module_from_spec(spec)
    # Postponed annotations resolve through sys.modules, as for a real import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["ee", "ce"])
def client(request, env, monkeypatch):  # noqa: F811
    db, _ = env
    if request.param == "ce" and not _CE_ROUTES.is_file():
        pytest.skip("CE overlay is only present in the main repository; a CE tree tests it as api.routes")
    routes = ee_routes if request.param == "ee" else _load_ce_routes()
    monkeypatch.setattr(routes, "local_mode_enabled", lambda: True)
    app = FastAPI()
    app.include_router(routes.router)
    state = {"user": "alice"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=state["user"], user_center_id=state["user"], username=state["user"]
    )
    test_client = TestClient(app)
    test_client.routes = routes
    test_client.state = state
    return test_client


def _resolve(client, path, **body):
    return client.post("/v1/projects/local/resolve", json={"path": str(path), **body})


def test_resolve_creates_then_reuses_including_symlinked_path(client, tmp_path):
    folder = tmp_path / "work" / "my-repo"
    folder.mkdir(parents=True)
    first = _resolve(client, folder, create=True)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["created"] is True
    assert data["name"] == "my-repo"
    assert data["local_path"] == str(folder.resolve())
    assert data["project_id"].startswith("prj_")

    again = _resolve(client, folder).json()["data"]
    assert again == {**data, "created": False}

    link = tmp_path / "link-to-repo"
    link.symlink_to(folder, target_is_directory=True)
    via_link = _resolve(client, str(link) + "/").json()["data"]
    assert via_link["project_id"] == data["project_id"]
    assert via_link["created"] is False


def test_resolve_matches_project_created_through_regular_route(client, env, tmp_path):  # noqa: F811
    db, _ = env
    folder = tmp_path / "existing"
    folder.mkdir()
    created = client.post(
        "/v1/projects", json={"name": "Existing", "kind": "local", "local_path": str(folder)}
    )
    assert created.status_code == 201, created.text
    data = _resolve(client, folder, name="ignored").json()["data"]
    assert data["project_id"] == created.json()["data"]["project_id"]
    assert data["name"] == "Existing" and data["created"] is False
    assert db.query(Project).filter(Project.kind == "local").count() == 1


def test_resolve_deduplicates_default_name_and_scopes_by_caller(client, env, tmp_path):  # noqa: F811
    one, two = tmp_path / "a" / "app", tmp_path / "b" / "app"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    first = _resolve(client, one).json()["data"]
    second = _resolve(client, two).json()["data"]
    assert (first["name"], second["name"]) == ("app", "app (2)")
    assert first["project_id"] != second["project_id"]

    named = tmp_path / "c"
    named.mkdir()
    assert _resolve(client, named, name="Custom").json()["data"]["name"] == "Custom"

    # Another user never sees alice's project for the same folder.
    client.state["user"] = "bob"
    other = _resolve(client, one).json()["data"]
    assert other["created"] is True and other["project_id"] != first["project_id"]


def test_resolve_without_create_returns_404_for_unknown_folder(client, env, tmp_path):  # noqa: F811
    db, _ = env
    folder = tmp_path / "fresh"
    folder.mkdir()
    assert _resolve(client, folder, create=False).status_code == 404
    assert db.query(Project).count() == 0


def test_resolve_is_404_outside_local_mode(client, env, monkeypatch, tmp_path):  # noqa: F811
    db, _ = env
    monkeypatch.setattr(client.routes, "local_mode_enabled", lambda: False)
    folder = tmp_path / "x"
    folder.mkdir()
    assert _resolve(client, folder).status_code == 404
    assert db.query(Project).count() == 0


@pytest.mark.parametrize("bad", ["relative/dir", "", "file", "missing"])
def test_resolve_rejects_bad_paths_with_422(client, env, tmp_path, bad):  # noqa: F811
    db, _ = env
    (tmp_path / "file").write_text("not a directory")
    path = {"file": tmp_path / "file", "missing": tmp_path / "missing"}.get(bad, bad)
    assert _resolve(client, path).status_code == 422
    assert db.query(Project).count() == 0


def test_resolve_with_grant_opens_the_folder_to_the_sandbox_once(client, tmp_path, monkeypatch):
    from features.desktop.local import grants

    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "home"))
    folder = tmp_path / "work" / "repo"
    (folder / "sub").mkdir(parents=True)
    response = client.post("/v1/projects/local/resolve", json={"path": str(folder), "grant": True})
    assert response.status_code == 200, response.text
    assert grants.list_grants() == [{"path": str(folder.resolve()), "mode": "readwrite"}]
    # A folder already inside a read-write grant adds nothing; without grant=true nothing is granted.
    client.post("/v1/projects/local/resolve", json={"path": str(folder / "sub"), "grant": True})
    client.post("/v1/projects/local/resolve", json={"path": str(tmp_path / "work")})
    assert len(grants.list_grants()) == 1


def test_workspace_grant_never_covers_home_or_root(tmp_path, monkeypatch):
    from features.desktop.local import grants

    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert grants.ensure_workspace_grant(str(tmp_path)) is False
    assert grants.ensure_workspace_grant("/") is False
    assert grants.list_grants() == []
