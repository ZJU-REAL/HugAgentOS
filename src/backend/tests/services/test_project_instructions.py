"""AGENTS.md acceptance tests against isolated SQLAlchemy and real disk storage."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.schemas import ChatRequest
from core.db.engine import Base
from core.db.models import Artifact, Project, UserFolder, UserShadow
from core.services.project_file_service import ProjectFileService
from core.services.project_init import resolve_project_init
from core.services.project_instructions import ProjectInstructionsService
from core.services.project_scope import build_project_ctx
from core.services.project_service import ProjectService


@pytest.fixture
def env(tmp_path, monkeypatch):
    # No application DB, object store, model, or sandbox process is used.
    engine = create_engine(f"sqlite:///{tmp_path / 'acceptance.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr("core.storage.factory._storage_instance", None)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    with factory() as db:
        db.add_all([UserShadow(user_id=u, username=u, email=f"{u}@example.com") for u in ("alice", "bob")])
        db.commit()
        yield db, tmp_path
    engine.dispose()


def local(env):
    db, root = env
    folder = root / "project"
    folder.mkdir()
    p = ProjectService(db).create_local("alice", "Local", str(folder))
    return p, folder / "AGENTS.md"


def update(db, p, text, revision=None):
    return ProjectService(db).update(p.project_id, "alice", {
        "instructions": text, "instructions_revision": revision,
    }, level="admin")


def test_local_file_and_editor_and_next_turn_share_one_source(env):
    db, _ = env
    p, path = local(env)
    p.instructions = "Legacy rules"
    db.commit()
    assert ProjectService(db).get(p.project_id, "alice")["instructions"] == "Legacy rules"
    path.write_text("# Existing rules\n保留此条\n", encoding="utf-8")
    snapshot = ProjectService(db).get(p.project_id, "alice")
    assert snapshot["instructions_source"] == "AGENTS.md"
    assert "保留此条" in snapshot["instructions"]
    # Missing slug avoids creating a real sandbox link during context assembly.
    p.extra_data = {**p.extra_data, "local": {"path": str(path.parent)}}
    db.commit()
    assert build_project_ctx(db, p.project_id)["project_instructions"] == path.read_text()
    updated = update(db, p, snapshot["instructions"] + "\nRun pytest.\n", snapshot["instructions_revision"])
    assert updated["instructions"] == path.read_text()
    path.write_text("External editor change\n")
    assert build_project_ctx(db, p.project_id)["project_instructions"] == "External editor change\n"
    path.unlink()
    assert ProjectService(db).get(p.project_id, "alice")["instructions"] == ""
    assert ProjectInstructionsService(db).read(p)["instructions_source"] == "missing"


def test_stale_editor_is_rejected_without_overwriting(env):
    db, _ = env
    p, path = local(env)
    old = ProjectInstructionsService(db).read(p)
    path.write_text("New external rule")
    with pytest.raises(HTTPException) as err:
        update(db, p, "stale", old["instructions_revision"])
    assert err.value.status_code == 409
    assert path.read_text() == "New external rule"


@pytest.mark.parametrize("content,status", [(b"\xff", 422), (b"a\0b", 422), (b"x" * 32769, 413)])
def test_invalid_root_rules_fail_explicitly(env, content, status):
    db, _ = env
    p, path = local(env)
    path.write_bytes(content)
    with pytest.raises(HTTPException) as err:
        ProjectService(db).get(p.project_id, "alice")
    assert err.value.status_code == status


def test_bom_empty_and_byte_limit(env):
    db, _ = env
    p, path = local(env)
    path.write_bytes(b"\xef\xbb\xbf# Rules")
    assert ProjectInstructionsService(db).read(p)["instructions"] == "# Rules"
    update(db, p, "")
    assert path.read_bytes() == b""
    update(db, p, "a" * 32768)
    assert path.stat().st_size == 32768
    with pytest.raises(HTTPException) as err:
        update(db, p, "中" * 11000)
    assert err.value.status_code == 413


@pytest.mark.parametrize("broken", [False, True])
def test_symlink_cannot_read_or_overwrite_outside_project(env, broken):
    db, root = env
    p, path = local(env)
    outside = root / "outside.md"
    if not broken:
        outside.write_text("Do not overwrite")
    path.symlink_to(outside)
    for operation in (lambda: ProjectInstructionsService(db).read(p), lambda: update(db, p, "unsafe")):
        with pytest.raises(HTTPException) as err:
            operation()
        assert err.value.status_code == 409
    assert path.is_symlink()
    assert broken or outside.read_text() == "Do not overwrite"


def test_cloud_root_scope_and_artifact_identity(env):
    db, _ = env
    svc = ProjectService(db)
    outer = UserFolder(folder_id="outer", user_id="alice", name="Outer")
    nested = UserFolder(folder_id="nested", user_id="alice", name="Same", parent_folder_id="outer")
    same = UserFolder(folder_id="same", user_id="alice", name="Same")
    db.add_all([outer, nested, same])
    db.commit()
    p = svc.create_personal("alice", "Nested", linked_folder_id="nested")
    other = svc.create_personal("alice", "Root", linked_folder_id="same")
    files = ProjectFileService(db)
    files.upload(p, "alice", b"Subdirectory rules", "sub/AGENTS.md", "text/markdown")
    files.upload(other, "alice", b"Other project rules", "AGENTS.md", "text/markdown")
    assert svc.get(p.project_id, "alice")["instructions"] == ""
    item = files.upload(p, "alice", b"# Root\nPreserve", "AGENTS.md", "text/markdown")
    snapshot = svc.get(p.project_id, "alice")
    art = db.get(Artifact, item["artifact_id"])
    art.parsed_text, art.summary = "stale sandbox content", "stale summary"
    db.commit()
    update(db, p, "# Updated\nPreserve", snapshot["instructions_revision"])
    assert db.get(Artifact, item["artifact_id"]).parsed_text is None
    assert len([f for f in files.list_files(p) if f["name"] == "AGENTS.md"]) == 1
    assert build_project_ctx(db, p.project_id)["project_instructions"] == "# Updated\nPreserve"
    assert svc.get(other.project_id, "alice")["instructions"] == "Other project rules"
    art.deleted_at = datetime.utcnow()
    db.commit()
    assert svc.get(p.project_id, "alice")["instructions"] == ""


def test_duplicate_cloud_roots_fail_instead_of_selecting_arbitrarily(env):
    db, _ = env
    p = ProjectService(db).create_personal("alice", "Duplicates")
    f = ProjectFileService(db)
    f.upload(p, "alice", b"A", "AGENTS.md", "text/markdown")
    f.upload(p, "alice", b"B", "AGENTS.md", "text/markdown")
    with pytest.raises(HTTPException) as err:
        ProjectInstructionsService(db).read(p)
    assert err.value.status_code == 409


@pytest.mark.parametrize("message", ["/init", "/初始化指令", "  /init  "])
def test_init_resolves_current_project_and_preserves_existing_rules(env, message):
    db, _ = env
    p = ProjectService(db).create_personal("alice", "Research")
    p.instructions = "不可删除的已有约定"
    db.commit()
    request = ChatRequest(chat_id="new-chat", message=message, project_id=p.project_id)
    prompt = resolve_project_init(db, request, "alice")
    assert "不可删除的已有约定" in prompt
    assert "read_project_instructions" in prompt and "save_project_instructions" in prompt
    assert "32 KiB" in prompt
    assert request.message == message.strip()


@pytest.mark.parametrize("message", ["Discuss /init", "/init more", "/initialize", "normal"])
def test_ordinary_messages_do_not_trigger_init(env, message):
    db, _ = env
    assert resolve_project_init(db, ChatRequest(chat_id="new", message=message), "alice") is None


def test_default_and_unauthorized_projects_reject_init(env):
    db, _ = env
    with pytest.raises(HTTPException) as err:
        resolve_project_init(db, ChatRequest(chat_id="new", message="/init"), "alice")
    assert err.value.status_code == 400
    p = ProjectService(db).create_personal("alice", "Private")
    with pytest.raises(HTTPException) as err:
        resolve_project_init(db, ChatRequest(chat_id="new", message="/init", project_id=p.project_id), "bob")
    assert err.value.status_code == 404


@pytest.mark.parametrize("mode", ["plan_chat", "batch_chat", "workflow_chat", "skill_id", "plugin_id", "agent_id", "mention_agent_id", "connector_id"])
def test_init_requires_unadorned_project_chat(env, mode):
    db, _ = env
    p = ProjectService(db).create_personal("alice", "Modes")
    req = ChatRequest(chat_id="new", message="/init", project_id=p.project_id, **{mode: True if mode.endswith("_chat") else "selected"})
    with pytest.raises(HTTPException) as err:
        resolve_project_init(db, req, "alice")
    assert err.value.status_code == 400


@pytest.mark.asyncio
async def test_registered_tools_save_real_cloud_content_with_revision_and_permissions(env):
    from agentscope.tool import Toolkit
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.project_instructions_tool import register_project_instruction_tools

    db, _ = env
    p = ProjectService(db).create_personal("alice", "Tools")
    collector = ToolCollector()
    register_project_instruction_tools(collector, project_id=p.project_id, user_id="alice")
    schemas = await Toolkit(tools=collector.function_tools).get_tool_schemas()
    schema = next(s["function"] for s in schemas if s["function"]["name"] == "save_project_instructions")
    assert set(schema["parameters"]["required"]) == {"content", "expected_revision"}
    # ToolCollector's documented get_tool exposes the registered function for direct execution.
    read = collector.get_tool("read_project_instructions")._func
    save = collector.get_tool("save_project_instructions")._func
    before = json.loads((await read()).content[0].text)
    result = json.loads((await save("# Rules\nTest first.", before["instructions_revision"])).content[0].text)
    assert result["ok"] is True
    assert result["instructions"] == "# Rules\nTest first."
    stale = json.loads((await save("overwrite", before["instructions_revision"])).content[0].text)
    assert stale["status"] == 409
    db.expire_all()
    assert build_project_ctx(db, p.project_id)["project_instructions"] == result["instructions"]
    p.deleted_at = datetime.utcnow()
    db.commit()
    denied = json.loads((await save("overwrite", result["instructions_revision"])).content[0].text)
    assert denied["status"] == 403


def test_saved_chat_binding_controls_initialization(env):
    from core.services.chat_service import ChatService

    db, _ = env
    svc = ProjectService(db)
    p = svc.create_personal("alice", "Bound")
    other = svc.create_personal("alice", "Other")
    chat = ChatService(db).ensure_session("bound-chat", "alice", project_id=p.project_id)
    req = ChatRequest(chat_id=chat.chat_id, message="/init")
    assert '"project_name": "Bound"' in resolve_project_init(db, req, "alice")
    assert req.project_id == p.project_id
    with pytest.raises(HTTPException) as err:
        resolve_project_init(db, ChatRequest(chat_id=chat.chat_id, message="/init", project_id=other.project_id), "alice")
    assert err.value.status_code == 409


def test_instructions_http_api_reads_writes_and_rejects_stale_or_unauthorized(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.v1.projects import router
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db

    db, _ = env
    p, path = local(env)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    actor = ["alice"]
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id=actor[0], user_center_id="test", username=actor[0])
    with TestClient(app) as client:
        url = f"/v1/projects/{p.project_id}"
        first = client.get(url)
        assert first.status_code == 200, first.text
        rev = first.json()["data"]["instructions_revision"]
        saved = client.patch(url + "/instructions", json={"instructions": "# Rules\n保留", "instructions_revision": rev})
        assert saved.status_code == 200, saved.text
        assert path.read_text() == "# Rules\n保留"
        assert client.patch(url + "/instructions", json={"instructions": "stale", "instructions_revision": rev}).status_code == 409
        assert client.patch(url + "/instructions", json={"instructions": "中" * 11000}).status_code == 413
        actor[0] = "bob"
        assert client.patch(url + "/instructions", json={"instructions": "intrusion"}).status_code in (403, 404)
        assert path.read_text() == "# Rules\n保留"


@pytest.mark.asyncio
async def test_team_initialization_tool_and_viewer_denial(env):
    # EE-only scenario; the common tests above also run in the derived CE tree.
    import core.db.models as models
    if not hasattr(models, 'Team'):
        pytest.skip("EE organization project")
    from core.db.models import Team, TeamMember
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.project_instructions_tool import register_project_instruction_tools

    db, _ = env
    db.add(Team(team_id="team", name="Team", owner_user_id="alice", source="manual"))
    db.add_all([
        TeamMember(team_id="team", user_id="alice", role="owner", file_permission="editor"),
        TeamMember(team_id="team", user_id="bob", role="member", file_permission="viewer"),
    ])
    db.commit()
    p = ProjectService(db).create_team("alice", "team", "Shared")
    denied = ChatRequest(chat_id="new", message="/init", project_id=p.project_id)
    with pytest.raises(HTTPException) as err:
        resolve_project_init(db, denied, "bob")
    assert err.value.status_code == 403
    collector = ToolCollector()
    register_project_instruction_tools(collector, project_id=p.project_id, user_id="alice")
    read = collector.get_tool("read_project_instructions")._func
    save = collector.get_tool("save_project_instructions")._func
    snapshot = json.loads((await read()).content[0].text)
    result = json.loads((await save("Shared project rules", snapshot["instructions_revision"])).content[0].text)
    assert result["ok"] is True
    db.expire_all()
    assert ProjectService(db).get(p.project_id, "bob")["instructions"] == "Shared project rules"
    with pytest.raises(HTTPException) as err:
        ProjectInstructionsService(db).write(p, "bob", "unauthorized")
    assert err.value.status_code == 403


def test_file_adoption_does_not_commit_unrelated_changes_or_restore_stale_metadata(env):
    from sqlalchemy.orm import Session
    db, _ = env
    p, path = local(env)
    p.instructions = "Legacy"
    p.extra_data = {**p.extra_data, "memory_enabled": True}
    db.commit()
    # Hold an old project snapshot while another request updates the switch.
    _ = p.extra_data
    with Session(bind=db.get_bind()) as other:
        latest = other.get(Project, p.project_id)
        latest.extra_data = {**latest.extra_data, "memory_enabled": False}
        other.commit()
    pending = UserShadow(user_id="uncommitted", username="pending", email="pending@example.com")
    db.add(pending)
    path.write_text("Adopted rules")
    assert ProjectInstructionsService(db).read(p)["instructions"] == "Adopted rules"
    with Session(bind=db.get_bind()) as verify:
        assert verify.get(UserShadow, "uncommitted") is None
        assert verify.get(Project, p.project_id).extra_data["memory_enabled"] is False
    db.rollback()
    path.unlink()
    assert ProjectInstructionsService(db).read(p)["instructions"] == ""


def test_simultaneous_local_saves_have_exactly_one_winner(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session

    db, _ = env
    p, path = local(env)
    update(db, p, "Initial")
    revision = ProjectInstructionsService(db).read(p)["instructions_revision"]
    project_id, engine = p.project_id, db.get_bind()
    start = Barrier(2)

    def worker(text):
        with Session(bind=engine) as session:
            project = session.get(Project, project_id)
            start.wait(timeout=10)
            try:
                ProjectInstructionsService(session).write(project, "alice", text, expected_revision=revision)
                session.commit()
                return 200, text
            except HTTPException as err:
                session.rollback()
                return err.status_code, text

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ("First", "Second")))
    assert sorted(status for status, _ in results) == [200, 409]
    assert path.read_text() == next(text for status, text in results if status == 200)

def test_chat_dispatch_expands_command_and_loads_fresh_rules(env):
    from api.routes.v1.chats import _resolve_chat_agent_targets, _build_ctx

    db, _ = env
    p = ProjectService(db).create_personal("alice", "Dispatch")
    req = ChatRequest(chat_id="new", message="/初始化指令", project_id=p.project_id, mode_slug="turbo", chat_mode="turbo")
    routed, agent, execution, subcommand = _resolve_chat_agent_targets(db, req, "alice")
    assert execution != req.message and "save_project_instructions" in execution
    assert routed.message == "/初始化指令"
    assert agent is None and subcommand is None
    assert routed.mode_slug == "standard" and routed.chat_mode != "turbo"
    ctx = _build_ctx(routed, "alice", [], [], [])
    assert ctx["project_init"] is True and ctx["project_id"] == p.project_id
    update(db, p, "Latest rules after initialization")
    normal = ChatRequest(chat_id="new", message="Continue work", project_id=p.project_id)
    ctx = _build_ctx(normal, "alice", [], [], [])
    assert ctx["project_instructions"] == "Latest rules after initialization"
    assert ctx["project_init"] is False


def test_combined_project_patch_preserves_metadata_with_production_session_settings(env):
    db, _ = env
    assert db.autoflush is False
    p, path = local(env)
    rev = ProjectInstructionsService(db).read(p)["instructions_revision"]
    result = ProjectService(db).update(p.project_id, "alice", {
        "instructions": "New instructions", "instructions_revision": rev,
        "name": "Renamed", "description": "New description", "memory_enabled": False,
    }, level="admin")
    assert result["name"] == "Renamed"
    assert result["description"] == "New description"
    assert result["memory_enabled"] is False
    assert path.read_text() == "New instructions"


@pytest.mark.asyncio
async def test_normal_chat_can_read_canonical_rules_but_has_no_init_write_tool(env):
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.project_instructions_tool import register_project_instruction_tools

    db, _ = env
    p = ProjectService(db).create_personal("alice", "Normal")
    update(db, p, "Current rules")
    collector = ToolCollector()
    register_project_instruction_tools(
        collector, project_id=p.project_id, user_id="alice", allow_write=False,
    )
    assert collector.get_tool("save_project_instructions") is None
    read = collector.get_tool("read_project_instructions")._func
    assert json.loads((await read()).content[0].text)["instructions"] == "Current rules"
    # Permission is checked at execution time, not only at registration.
    p.owner_user_id = "bob"
    db.commit()
    assert json.loads((await read()).content[0].text)["status"] == 403


@pytest.mark.asyncio
async def test_read_tool_ignores_stale_root_cache_without_changing_nested_rules(env, monkeypatch):
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.read_tool import register_read
    from core.llm.tools._state import ReadStateTracker
    from core.services.project_scope import project_scope_from_context

    db, _ = env
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    p = ProjectService(db).create_personal("alice", "ReadRoot")
    update(db, p, "FRESH-628")
    scope = project_scope_from_context(build_project_ctx(db, p.project_id))
    cache = {}
    class Provider:
        async def get_file(self, session, path, **kwargs):
            return cache.get(path, b"STALE-314")
        async def put_file(self, session, path, data, **kwargs):
            cache[path] = data
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Provider())
    collector = ToolCollector()
    register_read(collector, chat_id="read-init-test", user_id="alice",
                  state=ReadStateTracker(), project_folder_name=scope.folder_name, scope=scope)
    read = collector.get_tool("Read")._func
    root = f"/myspace/{scope.folder_name}/AGENTS.md"
    result = json.loads((await read(root)).content[0].text)
    assert "FRESH-628" in result["content"] and "STALE" not in result["content"]
    assert list(cache.values()) == [b"FRESH-628"]
    nested = json.loads((await read(f"/myspace/{scope.folder_name}/child/AGENTS.md")).content[0].text)
    assert "STALE-314" in nested["content"]
    # Removing the canonical file must not revive the disposable sandbox copy.
    art = ProjectInstructionsService(db)._artifact(p)
    art.deleted_at = datetime.utcnow()
    db.commit()
    deleted = json.loads((await read(root)).content[0].text)
    assert "不存在" in deleted["error"]
    p.owner_user_id = "bob"
    db.commit()
    assert json.loads((await read(root)).content[0].text)["status"] == 403
