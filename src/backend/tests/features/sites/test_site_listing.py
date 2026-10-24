"""Cloud site discovery: the agent gets an explicit site_id plus where to edit the source."""

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    from core.db.engine import Base
    from core.db.models import ChatSession, Project, UserFolder, UserShadow
    from features.sites.models import Site
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'cloud.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)

    base = datetime(2026, 9, 1, 12, 0, 0)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner"))
        db.add(UserFolder(folder_id="folder-a", user_id="owner", name="产品介绍站"))
        db.add(UserFolder(folder_id="folder-b", user_id="owner", name="data-board"))
        db.add(
            Project(
                project_id="proj-a",
                name="产品介绍站",
                kind="personal",
                owner_user_id="owner",
                linked_folder_id="folder-a",
            )
        )
        db.add(
            Project(
                project_id="proj-b",
                name="data-board",
                kind="personal",
                owner_user_id="owner",
                linked_folder_id="folder-b",
            )
        )
        db.add(ChatSession(chat_id="chat-in-a", user_id="owner", project_id="proj-a"))
        db.add(ChatSession(chat_id="chat-loose", user_id="owner"))
        db.add(
            Site(
                site_id="site-static",
                slug="intro",
                user_id="owner",
                project_id="proj-a",
                title="产品介绍",
                entry_file="index.html",
                current_version=3,
                updated_at=base - timedelta(days=2),
            )
        )
        db.add(
            Site(
                site_id="site-build",
                slug="board",
                user_id="owner",
                project_id="proj-b",
                title="数据看板",
                entry_file="index.html",
                current_version=1,
                extra_data={"build": {"kind": "build", "source_dir": "/workspace/board"}},
                updated_at=base,
            )
        )
        db.add(
            Site(
                site_id="site-legacy",
                slug="old",
                user_id="owner",
                title="老站点",
                entry_file="index.html",
                current_version=1,
                updated_at=base - timedelta(days=5),
            )
        )
        db.commit()
    return factory


def test_loose_chat_still_finds_every_editable_site(cloud):
    from features.sites.listing import list_sites

    entries = {e["site_id"]: e for e in list_sites("owner", "chat-loose")}
    assert set(entries) == {"site-static", "site-build", "site-legacy"}
    assert all(e["in_current_project"] is False for e in entries.values())


def test_source_dir_is_the_logical_myspace_path_the_agent_can_type(cloud):
    from features.sites.listing import list_sites

    entry = next(e for e in list_sites("owner", "chat-loose") if e["site_id"] == "site-static")
    assert entry["source_dir"] == "/myspace/产品介绍站"
    assert entry["publish_dir"] == "/myspace/产品介绍站"
    assert entry["kind"] == "static"
    assert entry["url"] == "/site/intro/"
    assert entry["version"] == 3
    assert entry["editable"] is True


def test_build_sites_report_no_publish_dir_so_the_agent_rebuilds(cloud):
    from features.sites.listing import list_sites

    entry = next(e for e in list_sites("owner", "chat-loose") if e["site_id"] == "site-build")
    assert entry["kind"] == "build"
    assert entry["source_dir"] == "/myspace/data-board"
    assert entry["publish_dir"] == ""


def test_site_without_source_project_is_reported_uneditable(cloud):
    from features.sites.listing import list_sites

    entry = next(e for e in list_sites("owner", "chat-loose") if e["site_id"] == "site-legacy")
    assert entry["editable"] is False
    assert entry["source_dir"] == ""


def test_current_project_site_sorts_first(cloud):
    from features.sites.listing import list_sites

    entries = list_sites("owner", "chat-in-a")
    assert entries[0]["site_id"] == "site-static"
    assert entries[0]["in_current_project"] is True
    assert all(e["in_current_project"] is False for e in entries[1:])


def test_listing_is_scoped_to_the_asking_account(cloud):
    from features.sites.listing import list_sites

    assert list_sites("someone-else", "chat-loose") == []
    assert list_sites("", "chat-loose") == []


def test_internal_endpoint_serves_the_mcp_tool(cloud, monkeypatch):
    from features.sites.api import internal_sites
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("BACKEND_INTERNAL_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(internal_sites.router)
    client = TestClient(app)

    payload = client.post(
        "/v1/internal/sites/list", json={"user_id": "owner", "chat_id": "chat-in-a"}
    ).json()["data"]
    assert payload["ok"] is True
    assert payload["count"] == 3
    assert payload["items"][0]["site_id"] == "site-static"

    anonymous = client.post("/v1/internal/sites/list", json={}).json()["data"]
    assert "error" in anonymous


def test_limit_is_clamped_to_a_context_safe_window(cloud):
    from features.sites.listing import list_sites

    assert len(list_sites("owner", "chat-loose", limit=1)) == 1
    assert len(list_sites("owner", "chat-loose", limit=999)) == 3
