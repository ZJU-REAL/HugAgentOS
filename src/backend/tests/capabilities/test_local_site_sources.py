"""Desktop source preparation and editing are local, durable and account scoped."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.capabilities.test_local_project_site_publish import local_project


import pytest


def test_prepare_site_creates_real_local_project_before_generation(local_project, monkeypatch):
    root, factory = local_project
    from api.routes.v1 import local_site_sources as route
    from core.auth.backend import get_current_user
    from types import SimpleNamespace

    monkeypatch.setattr("core.llm.tools._paths.WORKSPACE_ROOT", str(root.parent / "managed"))
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="owner")
    response = TestClient(app).post("/v1/local/site-sources/prepare", json={"title": "My site"})
    assert response.status_code == 200, response.text
    prepared = response.json()["data"]
    assert Path(prepared["source_dir"]).is_dir()
    from core.services.project_service import ProjectService

    with factory() as db:
        project = ProjectService(db).get(prepared["project_id"], "owner")
        assert project["kind"] == "local"


def test_publish_receipt_enables_local_edit_and_reuses_cloud_site(local_project, monkeypatch):
    root, _ = local_project
    from core.services import local_site_sources as sources
    from core.services.desktop_site_publish import package_local_site, localize_site_result

    monkeypatch.setattr(sources, "current_cloud", lambda: ("https://cloud.example", "cloud-user"))

    async def pack(src, *_args, **_kwargs):
        return [("index.html", (Path(src) / "index.html").read_bytes())], None

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)
    args = {"src_dir": str(root), "title": "Desktop"}
    headers = {"x-current-user-id": "owner", "x-chat-id": "chat"}
    asyncio.run(package_local_site(args, headers))
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ok": True,
                        "site_id": "cloud-site",
                        "url": "/site/report/",
                        "title": "Desktop",
                    }
                ),
            }
        ]
    }
    localize_site_result(result, "https://cloud.example", args, headers)
    entries = sources.list_sources("owner")
    assert len(entries) == 1
    assert entries[0]["site_id"] == "cloud-site"
    assert entries[0]["source_dir"] == str(root)
    assert entries[0]["project_id"] == "project"
    assert sources.open_editor("owner", "cloud-site")["chat_id"] == "chat"
    (root / "index.html").write_text("<h1>edited locally</h1>")
    _, options = asyncio.run(package_local_site({}, headers))
    assert json.loads(options)["site_id"] == "cloud-site"
    assert sources.list_sources("stranger") == []
    monkeypatch.setattr(sources, "current_cloud", lambda: ("https://other.example", "cloud-user"))
    assert sources.list_sources("owner") == []


def test_editor_reuses_replacement_when_original_chat_was_deleted(local_project, monkeypatch):
    root, factory = local_project
    from core.services import local_site_sources as sources
    from core.db.models import ChatSession
    from datetime import datetime

    monkeypatch.setattr(sources, "current_cloud", lambda: ("https://cloud.example", "cloud-user"))
    sources.save_receipt(
        "owner",
        "chat",
        {"project_id": "project", "source_dir": str(root), "publish_dir": str(root)},
        {"site_id": "site"},
        "https://cloud.example",
    )
    with factory() as db:
        db.get(ChatSession, "chat").deleted_at = datetime.utcnow()
        db.commit()
    first = sources.open_editor("owner", "site")
    second = sources.open_editor("owner", "site")
    assert first["chat_id"] != "chat"
    assert second["chat_id"] == first["chat_id"]


@pytest.mark.parametrize("url", ["/site/report/", "https://cloud.example/site/report/"])
def test_known_cloud_success_survives_local_receipt_failure(local_project, monkeypatch, url):
    root, _ = local_project
    from core.services.desktop_site_publish import localize_site_result
    from fastapi import HTTPException

    def fail(*args, **kwargs):
        raise HTTPException(403, "project removed during upload")

    monkeypatch.setattr("core.services.local_site_sources.save_receipt", fail)
    result = {
        "content": [
            {"type": "text", "text": json.dumps({"ok": True, "site_id": "cloud-site", "url": url})}
        ]
    }
    localize_site_result(
        result,
        "https://cloud.example",
        {
            "_desktop_source": {
                "project_id": "project",
                "source_dir": str(root),
                "publish_dir": str(root),
            }
        },
        {"x-current-user-id": "owner", "x-chat-id": "chat"},
    )
    published = json.loads(result["content"][0]["text"])
    assert published["ok"] is True
    assert published["site_id"] == "cloud-site"
    assert published["url"] == "https://cloud.example/site/report/"
    assert published["local_source_warning"]


def test_reassociation_moves_site_binding_to_one_project(local_project, monkeypatch):
    root, factory = local_project
    from core.services import local_site_sources as sources
    from core.db.models import Project, ChatSession

    monkeypatch.setattr(sources, "current_cloud", lambda: ("https://cloud.example", "cloud-user"))
    sources.save_receipt(
        "owner",
        "chat",
        {"project_id": "project", "source_dir": str(root), "publish_dir": str(root)},
        {"site_id": "site"},
        "https://cloud.example",
    )
    other = root.parent / "other-project"
    other.mkdir()
    (other / "index.html").write_text("second source")
    with factory() as db:
        db.add(
            Project(
                project_id="other",
                name="Other",
                kind="local",
                owner_user_id="owner",
                extra_data={"local": {"path": str(other)}},
            )
        )
        db.add(ChatSession(chat_id="other-chat", user_id="owner", project_id="other"))
        db.commit()
    sources.save_receipt(
        "owner",
        "other-chat",
        {"project_id": "other", "source_dir": str(other), "publish_dir": str(other)},
        {"site_id": "site"},
        "https://cloud.example",
    )
    assert len(sources.list_sources("owner")) == 1
    assert sources.open_editor("owner", "site")["project_id"] == "other"


def test_source_must_already_be_inside_local_project(local_project):
    root, _ = local_project
    from core.services.local_site_sources import validate_source

    outside = root.parent / "scratch-site"
    outside.mkdir()
    (outside / "index.html").write_text("scratch")
    with pytest.raises(ValueError, match="当前本地项目"):
        validate_source("owner", "chat", str(outside), str(root))
    with pytest.raises(ValueError, match="当前本地项目"):
        validate_source("owner", "chat", str(root), str(outside))
    assert validate_source("owner", "chat", str(root), str(root))["project_id"] == "project"


def test_local_publish_requires_project_chat(local_project, monkeypatch):
    from core.services.desktop_site_publish import package_local_site

    async def unexpected_pack(*args, **kwargs):
        pytest.fail("must reject before reading files")

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", unexpected_pack)
    with pytest.raises(ValueError, match="已绑定本地项目的会话"):
        asyncio.run(package_local_site({}, {"x-current-user-id": "owner"}))
