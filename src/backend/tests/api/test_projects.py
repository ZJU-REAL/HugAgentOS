"""Project module unit tests (project ↔ MySpace folder hard-binding version).

Covers:
- Creating a personal project auto-creates a user_folder of the same name
- Duplicate project name rejected
- Specifying an existing folder at creation
- Rejected when the folder is already bound to another project
- Team member rejected from creating a team project; owner passes + team subfolder auto-created
- Uploaded files land directly in the bound folder (also appear in the MySpace list)
- Uploading a file with a path auto-mkdirs subfolders
- The in-project file list is the bound folder's subtree
- Soft-deleting a project does not delete the bound folder itself
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.db.models import (
    Artifact,
    Project,
    Team,
    TeamMember,
    UserFolder,
    UserShadow,
)
from core.db.repository import ArtifactRepository
from core.services.project_file_service import ProjectFileService
from core.services.project_service import ProjectService


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def alice(db_session):
    u = UserShadow(user_id="user_alice", username="Alice", email="alice@example.com")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def bob(db_session):
    u = UserShadow(user_id="user_bob", username="Bob", email="bob@example.com")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def alice_team(db_session, alice, bob):
    team = Team(team_id="team_acme", name="Acme", owner_user_id=alice.user_id, source="manual")
    db_session.add(team)
    db_session.add(TeamMember(team_id=team.team_id, user_id=alice.user_id, role="owner", file_permission="editor"))
    db_session.add(TeamMember(team_id=team.team_id, user_id=bob.user_id, role="member", file_permission="viewer"))
    db_session.commit()
    return team


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    store: dict[str, bytes] = {}

    class _Fake:
        def upload_bytes(self, data: bytes, key: str) -> str:
            store[key] = data
            return f"local://{key}"

        def download_bytes(self, key: str) -> bytes:
            return store[key]

        def delete(self, key: str) -> None:
            store.pop(key, None)

    from core.services import project_file_service as pfs

    monkeypatch.setattr(pfs, "get_storage", lambda: _Fake())


# ── Create / linked folder ───────────────────────────────────────────────


def test_create_personal_auto_creates_folder(db_session, alice):
    svc = ProjectService(db_session)
    p = svc.create_personal(alice.user_id, name="P1", description="hi")
    assert p.linked_folder_id is not None
    folder = db_session.query(UserFolder).filter(UserFolder.folder_id == p.linked_folder_id).first()
    assert folder is not None
    assert folder.name == "P1"
    assert folder.parent_folder_id is None


def test_create_personal_duplicate_folder_name_appends_suffix(db_session, alice):
    """When creating a second project with the same name, the auto-created folder appends a suffix to avoid conflict."""
    svc = ProjectService(db_session)
    p1 = svc.create_personal(alice.user_id, name="Demo")
    # Delete p1 (so a same-named project can be created), but the folder is kept
    svc.soft_delete(p1.project_id, alice.user_id)
    p2 = svc.create_personal(alice.user_id, name="Demo")
    folder = db_session.query(UserFolder).filter(UserFolder.folder_id == p2.linked_folder_id).first()
    assert folder.name == "Demo (2)"  # does not conflict with the existing "Demo"


def test_create_personal_with_existing_folder(db_session, alice):
    folder = UserFolder(folder_id="ufld_x", user_id=alice.user_id, parent_folder_id=None, name="Already")
    db_session.add(folder)
    db_session.commit()
    svc = ProjectService(db_session)
    p = svc.create_personal(alice.user_id, name="UseExisting", linked_folder_id=folder.folder_id)
    assert p.linked_folder_id == folder.folder_id


def test_existing_folder_used_by_another_project_rejected(db_session, alice):
    folder = UserFolder(folder_id="ufld_y", user_id=alice.user_id, parent_folder_id=None, name="Shared")
    db_session.add(folder)
    db_session.commit()
    svc = ProjectService(db_session)
    svc.create_personal(alice.user_id, name="First", linked_folder_id=folder.folder_id)
    with pytest.raises(HTTPException) as exc:
        svc.create_personal(alice.user_id, name="Second", linked_folder_id=folder.folder_id)
    assert exc.value.status_code == 400


def test_duplicate_personal_project_name_rejected(db_session, alice):
    svc = ProjectService(db_session)
    svc.create_personal(alice.user_id, name="Dup")
    with pytest.raises(HTTPException):
        svc.create_personal(alice.user_id, name="Dup")


# ── Team project ─────────────────────────────────────────────────────────


def test_team_member_cannot_create_team_project(db_session, alice_team, bob):
    svc = ProjectService(db_session)
    with pytest.raises(HTTPException) as exc:
        svc.create_team(bob.user_id, alice_team.team_id, name="Bob")
    assert exc.value.status_code == 403


def test_team_owner_creates_team_project_auto_team_folder(db_session, alice_team, alice, bob):
    svc = ProjectService(db_session)
    p = svc.create_team(alice.user_id, alice_team.team_id, name="TeamP")
    assert p.linked_team_folder_id is not None
    from core.db.models import TeamFolder
    f = db_session.query(TeamFolder).filter(TeamFolder.folder_id == p.linked_team_folder_id).first()
    assert f.name == "TeamP"
    assert f.team_id == alice_team.team_id

    # Bob is a member and can see the team project
    items, total = svc.list_visible(bob.user_id)
    assert total == 1
    assert items[0]["folder_name"] == "TeamP"


# ── File operations ──────────────────────────────────────────────────────


def test_upload_file_lands_in_linked_folder(db_session, alice):
    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="UPL")
    pfs = ProjectFileService(db_session)
    item = pfs.upload(project, alice.user_id, b"hello", "a.txt", "text/plain")
    assert item["artifact_id"].startswith("pj_")

    art = db_session.query(Artifact).filter(Artifact.artifact_id == item["artifact_id"]).first()
    assert art.user_folder_id == project.linked_folder_id


def test_upload_with_subpath_creates_nested_folder(db_session, alice):
    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="NestedUp")
    pfs = ProjectFileService(db_session)
    item = pfs.upload(project, alice.user_id, b"x", "sub/deep/x.txt", "text/plain")
    # Both subfolders 'sub' and 'sub/deep' should be created
    sub = (
        db_session.query(UserFolder)
        .filter(
            UserFolder.user_id == alice.user_id,
            UserFolder.parent_folder_id == project.linked_folder_id,
            UserFolder.name == "sub",
            UserFolder.deleted_at.is_(None),
        )
        .first()
    )
    assert sub is not None
    deep = (
        db_session.query(UserFolder)
        .filter(
            UserFolder.user_id == alice.user_id,
            UserFolder.parent_folder_id == sub.folder_id,
            UserFolder.name == "deep",
            UserFolder.deleted_at.is_(None),
        )
        .first()
    )
    assert deep is not None
    art = db_session.query(Artifact).filter(Artifact.artifact_id == item["artifact_id"]).first()
    assert art.user_folder_id == deep.folder_id


def test_list_files_returns_subtree(db_session, alice):
    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="LST")
    pfs = ProjectFileService(db_session)
    pfs.upload(project, alice.user_id, b"a", "root.txt", "text/plain")
    pfs.upload(project, alice.user_id, b"b", "child/inner.txt", "text/plain")
    items = pfs.list_files(project)
    names = {it["name"] for it in items}
    assert "root.txt" in names
    assert "child/inner.txt" in names


def test_project_uploaded_files_visible_in_myspace(db_session, alice):
    """New design: project upload = MySpace upload, so it should also be visible in the main MySpace list."""
    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="MS")
    pfs = ProjectFileService(db_session)
    pfs.upload(project, alice.user_id, b"x", "z.txt", "text/plain")

    repo = ArtifactRepository(db_session)
    rows, total = repo.list_by_user_with_chat(user_id=alice.user_id, personal_only=True)
    assert total == 1
    assert rows[0]["artifact"].filename == "z.txt"


# ── Soft delete ──────────────────────────────────────────────────────────


def test_myspace_rel_under_project_scope_redirects():
    """With a personal project scope passed in, myspace_rel's output is prefixed with the
    bound folder name (without duplicating the prefix). scope is now an explicit parameter,
    no longer via ContextVar."""
    from core.llm.tools.myspace_vfs import myspace_rel
    from core.services.project_scope import ProjectScope

    scope = ProjectScope(
        project_id="prj_x", kind="personal",
        root_folder_id="ufld_x", folder_name="P1",
    )
    # Root directory
    assert myspace_rel("/myspace", "u1", scope) == "P1"
    # Subpath
    assert myspace_rel("/myspace/foo.txt", "u1", scope) == "P1/foo.txt"
    # Already under the project: no duplication
    assert myspace_rel("/myspace/P1/foo.txt", "u1", scope) == "P1/foo.txt"
    # Non-myspace paths still return None
    assert myspace_rel("/workspace/skills/x", "u1", scope) is None

    # Without scope, back to normal behavior
    assert myspace_rel("/myspace/foo.txt", "u1") == "foo.txt"


def test_project_scope_team_kind_also_prefixes():
    """A team-project scope likewise prefixes the bound folder name (so relative paths written by the LLM land under the project)."""
    from core.llm.tools.myspace_vfs import myspace_rel
    from core.services.project_scope import ProjectScope

    scope = ProjectScope(
        project_id="prj_t", kind="team",
        root_folder_id="fld_t", folder_name="TP",
        team_id="team_42",
    )
    assert myspace_rel("/myspace/x.txt", "u1", scope) == "TP/x.txt"


def test_soft_delete_project_keeps_folder(db_session, alice):
    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="Del")
    folder_id = project.linked_folder_id
    assert svc.soft_delete(project.project_id, alice.user_id) is True
    # The folder still exists (still accessible from the MySpace side)
    folder = db_session.query(UserFolder).filter(UserFolder.folder_id == folder_id).first()
    assert folder is not None
    assert folder.deleted_at is None


# ── _persist_artifacts auto-routing under project scope ──────────────────


def test_persist_artifacts_under_personal_project_scope_routes_to_folder(
    db_session, alice, monkeypatch,
):
    """With a personal project scope active, _persist_artifacts automatically routes the artifact to the bound folder.

    Covers the bash→sandbox_get_artifact→pin_to_workspace chain: previously the artifact
    row had no user_folder_id and the file showed up in the "My Space" root directory
    instead of inside the project.
    """
    from api.routes.v1 import chats as chats_module
    from core.db.models import ChatSession
    from core.services.project_scope import ProjectScope

    svc = ProjectService(db_session)
    project = svc.create_personal(alice.user_id, name="Proj42")
    chat = ChatSession(chat_id="chat_pa1", user_id=alice.user_id, title="t")
    db_session.add(chat)
    db_session.commit()

    # _persist_artifacts internally queries db.query(ArtifactModel.artifact_id); the same
    # db_session must be used — just pass it in directly (the fixture already provides the
    # same engine bound to the session)
    collected = [{
        "file_id": "ai_chart_001",
        "name": "chart.png",
        "mime_type": "image/png",
        "size": 1234,
        "storage_key": "artifacts/ai_chart_001",
        "url": "/files/ai_chart_001",
        "tool_name": "pin_to_workspace",
    }]

    scope = ProjectScope(
        project_id=project.project_id,
        kind="personal",
        root_folder_id=project.linked_folder_id,
        folder_name="Proj42",
    )
    chats_module._persist_artifacts(
        db_session, alice.user_id, chat.chat_id, collected, scope=scope,
    )

    row = db_session.query(Artifact).filter(
        Artifact.artifact_id == "ai_chart_001",
    ).first()
    assert row is not None
    assert row.user_folder_id == project.linked_folder_id


def test_persist_artifacts_without_scope_stays_at_root(db_session, alice):
    """Non-project chats are unaffected: user_folder_id stays empty (lands in the MySpace root)."""
    from api.routes.v1 import chats as chats_module
    from core.db.models import ChatSession

    chat = ChatSession(chat_id="chat_nr1", user_id=alice.user_id, title="t")
    db_session.add(chat)
    db_session.commit()

    collected = [{
        "file_id": "ai_chart_002",
        "name": "chart.png",
        "mime_type": "image/png",
        "size": 1234,
        "storage_key": "artifacts/ai_chart_002",
        "url": "/files/ai_chart_002",
        "tool_name": "pin_to_workspace",
    }]
    chats_module._persist_artifacts(
        db_session, alice.user_id, chat.chat_id, collected,
    )
    row = db_session.query(Artifact).filter(
        Artifact.artifact_id == "ai_chart_002",
    ).first()
    assert row is not None
    assert row.user_folder_id is None


def test_persist_artifacts_under_team_scope_routes_to_team_folder(
    db_session, alice,
):
    """team scope: artifacts are written with team_id + team_folder_id, no longer leaking into the personal MySpace root.

    Regression guard: a past implementation early-returned on team scope (PR 1 read-only),
    compounded by chat_run_executor / automation_scheduler failing to pass scope, so team
    project AI output was written as orphan rows with user_folder_id=NULL/team_id=NULL
    landing in the personal root.
    """
    from api.routes.v1 import chats as chats_module
    from core.db.models import ChatSession, Team, TeamFolder
    from core.services.project_scope import ProjectScope

    # PostgreSQL enforces foreign keys: artifacts.team_id → teams.team_id and
    # artifacts.team_folder_id → team_folders.folder_id, so the corresponding rows must be
    # created first (previously optional under SQLite which doesn't enforce FKs; required
    # after switching to a real database).
    db_session.add(Team(team_id="team_xxx", name="TeamProj",
                        owner_user_id=alice.user_id, source="manual"))
    db_session.add(TeamFolder(folder_id="tfld_xxx", team_id="team_xxx",
                              name="TeamProj", created_by=alice.user_id))
    chat = ChatSession(chat_id="chat_tm1", user_id=alice.user_id, title="t")
    db_session.add(chat)
    db_session.commit()

    collected = [{
        "file_id": "ai_chart_003",
        "name": "chart.png",
        "mime_type": "image/png",
        "size": 1234,
        "storage_key": "artifacts/ai_chart_003",
        "url": "/files/ai_chart_003",
        "tool_name": "pin_to_workspace",
    }]
    scope = ProjectScope(
        project_id="prj_t",
        kind="team",
        root_folder_id="tfld_xxx",
        folder_name="TeamProj",
        team_id="team_xxx",
    )
    chats_module._persist_artifacts(
        db_session, alice.user_id, chat.chat_id, collected, scope=scope,
    )
    row = db_session.query(Artifact).filter(
        Artifact.artifact_id == "ai_chart_003",
    ).first()
    assert row is not None
    assert row.team_id == "team_xxx"
    assert row.team_folder_id == "tfld_xxx"
    assert row.user_folder_id is None
