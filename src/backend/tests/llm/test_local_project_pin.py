"""Local project delivery uses the original file through the public tools/API."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import sessionmaker

from core.db.models import Project, UserShadow, Artifact, ChatSession
from core.llm import workspace
from core.llm.tool_collector import ToolCollector
from core.llm.tools.pin_tool import register_pin_to_workspace
from core.services.project_scope import ProjectScope


@pytest.fixture
def local_project(tmp_path, db_session, monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_MODE", "1")
    from core.config import local_mode

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    from api.routes import files

    monkeypatch.setattr(files, "local_mode_enabled", lambda: True)
    from core.artifacts import store

    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "artifacts")
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db_session.get_bind()))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "home"))
    from core.services.local_grant_service import add_grant

    add_grant(str(root))
    source = root / "report.txt"
    source.write_text("original report")
    db_session.add(UserShadow(user_id="pin-user", username="Pin user"))
    db_session.add(
        Project(
            project_id="pin-project",
            name="Reports",
            kind="local",
            owner_user_id="pin-user",
            extra_data={"local": {"path": str(root), "slug": "reports"}},
        )
    )
    db_session.add(ChatSession(chat_id="pin-chat", user_id="pin-user", project_id="pin-project"))
    db_session.commit()
    from core.infra.logging import user_id_var, chat_id_var

    token = user_id_var.set("pin-user")
    chat_token = chat_id_var.set("")
    scope = ProjectScope(
        project_id="pin-project",
        kind="local",
        root_folder_id="",
        folder_name="reports",
        is_local=True,
        local_slug="reports",
        local_path=str(root),
    )
    from core.llm.tool_permissions import (
        CURRENT_PERMISSION_TICKET,
        PermissionTicket,
        PermissionIntent,
    )

    ticket_token = CURRENT_PERMISSION_TICKET.set(
        PermissionTicket(
            "sandbox_get_artifact",
            "export",
            "",
            "test-export",
            (PermissionIntent("local_path", "read", str(source), "export original"),),
        )
    )
    workspace.init_state()
    yield source, scope
    CURRENT_PERMISSION_TICKET.reset(ticket_token)
    user_id_var.reset(token)
    chat_id_var.reset(chat_token)


def pin_tool(scope, session_id=None):
    collector = ToolCollector()
    register_pin_to_workspace(collector, scope=scope, sandbox_session_id=session_id)

    async def invoke(**args):
        from core.llm.tool_permissions import (
            ToolPermissionRegistry,
            ToolPermissionService,
            PermissionRuntime,
            CURRENT_PERMISSION_TICKET,
        )

        registry = ToolPermissionRegistry()
        registry.register(
            "pin_to_workspace", collector.permission_specs["pin_to_workspace"], source="test"
        )
        service = ToolPermissionService(
            registry,
            PermissionRuntime(
                chat_id="pin-chat", user_id="pin-user", interactive=False, approval_available=False
            ),
        )
        outcome = await service.authorize(
            SimpleNamespace(name="pin_to_workspace", id="pin-call", input=args)
        )
        if not outcome.proceed:
            raise PermissionError(str(outcome.payload))
        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            return await collector.get_tool("pin_to_workspace")._func(**args)
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)

    return invoke


def test_pin_local_path_opens_original_and_deduplicates(local_project, db_session, tmp_path):
    source, scope = local_project
    pin = pin_tool(scope)
    result = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    assert result["ok"] and len(result["pinned"]) == 1
    fid = result["pinned"][0]["file_id"]
    again = json.loads(asyncio.run(pin(file_paths=["report.txt"])).content[0].text)
    assert again["pinned"][0]["file_id"] == fid
    assert again["pinned"][0]["already_pinned"]
    from api.routes.files import local_file_location, download_file

    user = SimpleNamespace(user_id="pin-user")
    assert local_file_location(fid, user, db_session)["path"] == str(source)
    source.write_text("updated report")
    response = download_file(
        fid, BackgroundTasks(), mode="direct", inline=True, user=user, db=db_session
    )
    assert Path(response.path).read_text() == "updated report"
    from core.services.artifact_service import persist_artifacts

    persist_artifacts(db_session, "pin-user", None, workspace.get_pinned(), scope=scope)
    assert db_session.query(Artifact).count() == 0
    assert not list((tmp_path / "artifacts").glob("*.txt"))
    with pytest.raises(HTTPException):
        local_file_location(fid, SimpleNamespace(user_id="other-user"), db_session)
    source.unlink()
    with pytest.raises(HTTPException):
        local_file_location(fid, user, db_session)


def test_existing_export_then_pin_does_not_copy_project_file(
    local_project, db_session, monkeypatch, tmp_path
):
    source, scope = local_project
    from core.llm.tools.sandbox_tool import register_sandbox_get_artifact
    from core.artifacts.store import get_artifact

    class Provider:
        async def get_file_to_path(self, session, src, dst, **kwargs):
            data = Path(src).read_bytes()
            Path(dst).write_bytes(data)
            return len(data)

    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Provider())
    collector = ToolCollector()
    register_sandbox_get_artifact(collector, chat_id="pin-chat", user_id="pin-user", scope=scope)
    export = collector.get_tool("sandbox_get_artifact")._func
    result = json.loads(asyncio.run(export(src_path=str(source))).content[0].text)
    assert result.get("ok"), result
    assert get_artifact(result["file_id"])["path"] == str(source)
    pin = pin_tool(scope)
    result = json.loads(asyncio.run(pin(file_ids=[result["file_id"]])).content[0].text)
    assert result["pinned_count"] == 1
    assert not list((tmp_path / "artifacts").glob("*.txt"))


def test_pinned_file_remains_readable_across_turns_and_saves_to_project(
    local_project, db_session, monkeypatch, tmp_path
):
    import io
    from fastapi import UploadFile
    from core.content.artifact_reader import fetch_parsed_text, load_artifact_meta
    from api.routes.v1.file_upload import overwrite_file

    source, scope = local_project
    fid = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(source)])).content[0].text)[
        "pinned"
    ][0]["file_id"]
    workspace.init_state()
    assert load_artifact_meta(fid, "pin-user")["name"] == "report.txt"
    assert fetch_parsed_text(fid, "pin-user") == "original report"
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "home"))
    result = asyncio.run(
        overwrite_file(
            fid,
            UploadFile(filename="report.txt", file=io.BytesIO(b"saved from Canvas")),
            SimpleNamespace(user_id="pin-user"),
            db_session,
        )
    )
    assert result["file_id"] == fid
    assert source.read_text() == "saved from Canvas"
    assert fetch_parsed_text(fid, "pin-user") == "saved from Canvas"
    assert fetch_parsed_text(fid, "other-user") == ""
    assert db_session.query(Artifact).count() == 0


@pytest.mark.parametrize("change", ["deleted", "rebound", "symlink"])
def test_reference_revalidates_project_and_file(local_project, db_session, tmp_path, change):
    from api.routes.files import local_file_location

    source, scope = local_project
    fid = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(source)])).content[0].text)[
        "pinned"
    ][0]["file_id"]
    project = db_session.query(Project).filter_by(project_id=scope.project_id).one()
    outside = tmp_path / "other-project"
    outside.mkdir()
    (outside / "report.txt").write_text("private outside file")
    if change == "deleted":
        from datetime import datetime, timezone

        project.deleted_at = datetime.now(timezone.utc)
    elif change == "rebound":
        project.extra_data = {"local": {"path": str(outside)}}
    else:
        source.unlink()
        source.symlink_to(outside / "report.txt")
    db_session.commit()
    with pytest.raises(HTTPException):
        local_file_location(fid, SimpleNamespace(user_id="pin-user"), db_session)
    with pytest.raises(PermissionError):
        asyncio.run(pin_tool(scope)(file_paths=[str(outside / "report.txt")]))


def test_revoked_folder_grant_blocks_pin_and_existing_reference(local_project, db_session):
    from core.llm.tool_permissions import CURRENT_PERMISSION_TICKET
    from core.services.local_grant_service import remove_grant
    from core.content.artifact_reader import fetch_parsed_text
    from api.routes.files import local_file_location

    source, scope = local_project
    fid = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(source)])).content[0].text)[
        "pinned"
    ][0]["file_id"]
    remove_grant(str(source.parent))
    token = CURRENT_PERMISSION_TICKET.set(None)
    try:
        with pytest.raises(PermissionError):
            asyncio.run(pin_tool(scope)(file_paths=["report.txt"]))
        assert fetch_parsed_text(fid, "pin-user") == ""
        with pytest.raises(HTTPException):
            local_file_location(fid, SimpleNamespace(user_id="pin-user"), db_session)
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)


def test_live_spreadsheet_sheet_read_and_artifact_input(local_project):
    import base64
    from openpyxl import Workbook
    from core.llm.tools.read_artifact_tool import register_read_artifact
    from core.llm.tools._tool_helpers import _resolve_artifact_files

    source, scope = local_project
    sheet = source.with_suffix(".xlsx")
    book = Workbook()
    book.active.title = "预算"
    book.active.append(["费用", 125])
    book.save(sheet)
    fid = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(sheet)])).content[0].text)[
        "pinned"
    ][0]["file_id"]
    collector = ToolCollector()
    register_read_artifact(collector, user_id="pin-user")
    response = json.loads(
        asyncio.run(collector.get_tool("read_artifact")._func(file_id=fid, sheet_name="预算"))
        .content[0]
        .text
    )
    assert response["sheet_names"] == ["预算"]
    assert "125" in response["content"]
    content, error = _resolve_artifact_files({"input.xlsx": fid}, "pin-user")
    assert error is None
    assert base64.b64decode(content["input.xlsx"]) == sheet.read_bytes()


def test_scratch_export_keeps_copy_behavior(local_project, monkeypatch, tmp_path):
    from core.llm.tools.sandbox_tool import register_sandbox_get_artifact
    from core.artifacts.store import get_artifact
    from core.artifacts.local_project import is_local_project_ref

    source, scope = local_project
    from core.llm.tools import _paths
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    scratch = root / "scratch.txt"
    scratch.write_text("scratch output")

    original_resolve = _paths.to_physical_path
    monkeypatch.setattr(
        _paths,
        "to_physical_path",
        lambda path, user, **kwargs: (
            str(scratch)
            if path == "/workspace/scratch/output.txt"
            else original_resolve(path, user, **kwargs)
        ),
    )

    class Provider:
        async def get_file_to_path(self, session, src, dst, **kwargs):
            Path(dst).write_bytes(Path(src).read_bytes())
            return Path(dst).stat().st_size

    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Provider())
    collector = ToolCollector()
    register_sandbox_get_artifact(collector, chat_id="pin-chat", user_id="pin-user", scope=scope)
    result = json.loads(
        asyncio.run(
            collector.get_tool("sandbox_get_artifact")._func(
                src_path="/workspace/scratch/output.txt"
            )
        )
        .content[0]
        .text
    )
    assert result.get("ok"), result
    assert not is_local_project_ref(result["file_id"])
    assert get_artifact(result["file_id"])["path"] != str(scratch)
    assert scratch.read_text() == "scratch output"


def test_write_delivers_original_project_file(local_project, monkeypatch):
    from core.llm.tools.write_tool import register_write
    from core.llm.tools._state import ReadStateTracker
    from core.llm.tool_permissions import (
        CURRENT_PERMISSION_TICKET,
        PermissionTicket,
        PermissionIntent,
    )
    from core.artifacts.store import get_artifact

    source, scope = local_project
    target = source.parent / "new-report.txt"
    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider", lambda: SimpleNamespace(name="script_runner")
    )
    collector = ToolCollector()
    register_write(
        collector, chat_id="pin-chat", user_id="pin-user", state=ReadStateTracker(), scope=scope
    )
    token = CURRENT_PERMISSION_TICKET.set(
        PermissionTicket(
            "Write",
            "write-call",
            "",
            "test-write",
            (PermissionIntent("local_path", "write", str(target), "create report"),),
        )
    )
    try:
        response = json.loads(
            asyncio.run(
                collector.get_tool("Write")._func(
                    file_path=str(target), content="created in project", register_as_artifact=True
                )
            )
            .content[0]
            .text
        )
        assert response.get("ok"), response
        pinned = workspace.get_pinned()
        assert len(pinned) == 1
        assert get_artifact(pinned[0]["file_id"])["path"] == str(target)
        assert target.read_text() == "created in project"
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)


@pytest.mark.parametrize("logical_alias", [False, True])
def test_export_rejects_project_symlink_escape(local_project, tmp_path, monkeypatch, logical_alias):
    from core.llm.tools.sandbox_tool import register_sandbox_get_artifact
    from core.llm.tools import _paths

    source, scope = local_project
    outside = tmp_path / "outside.txt"
    outside.write_text("outside project")
    source.unlink()
    source.symlink_to(outside)
    path = str(source)
    if logical_alias:
        path = "/workspace/local/reports/report.txt"
        original_resolve = _paths.to_physical_path
        monkeypatch.setattr(
            _paths,
            "to_physical_path",
            lambda value, user, **kwargs: (
                str(outside) if value == path else original_resolve(value, user, **kwargs)
            ),
        )
    collector = ToolCollector()
    register_sandbox_get_artifact(collector, chat_id="pin-chat", user_id="pin-user", scope=scope)
    result = json.loads(
        asyncio.run(collector.get_tool("sandbox_get_artifact")._func(src_path=path)).content[0].text
    )
    assert "error" in result and "file_id" not in result


def test_desktop_bridge_previews_pinned_html_through_http(local_project, db_session, monkeypatch):
    import base64
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import files
    from core.db.engine import get_db

    from core.config.settings import settings
    from dataclasses import replace
    monkeypatch.setattr("core.auth.backend.settings", replace(settings, auth=replace(settings.auth, mode="session")))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "fixture-only-bridge")
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    source, scope = local_project
    shadow = db_session.query(UserShadow).filter_by(user_id="pin-user").one()
    shadow.user_center_id = "preview-cloud-user"
    db_session.commit()
    html = source.with_suffix(".html")
    html.write_text("<!doctype html><meta charset='utf-8'><h1>本机预览</h1>", encoding="utf-8")
    result = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(html)])).content[0].text)
    fid = result["pinned"][0]["file_id"]
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_db] = lambda: db_session
    headers = {
        "x-desktop-bridge": "fixture-only-bridge",
        "x-desktop-bridge-user": base64.b64encode(json.dumps({
            "user_center_id": "preview-cloud-user", "username": "Preview user",
        }).encode()).decode(),
    }
    with TestClient(app) as client:
        location = client.get(f"/files/{fid}/local-path", headers=headers)
        assert location.status_code == 200, location.text
        response = client.get(f"/files/{fid}?inline=1", headers=headers)
        assert response.status_code == 200, response.text
        assert "本机预览" in response.text
        assert response.headers["content-type"].startswith("text/html")
        # A stale forwarded cloud cookie must not override the shell identity.
        assert client.get(f"/files/{fid}?inline=1", headers={**headers, "Cookie":"jx_session=expired-cloud-cookie"}).status_code == 200
        assert client.get(f"/files/{fid}?inline=1").status_code == 401
        bad_headers = {**headers, "x-desktop-bridge": "wrong"}
        assert client.get(f"/files/{fid}?inline=1", headers=bad_headers).status_code == 401
        other_headers = {**headers, "x-desktop-bridge-user": base64.b64encode(json.dumps({
            "user_center_id": "another-cloud-user", "username": "Other user",
        }).encode()).decode()}
        assert client.get(f"/files/{fid}?inline=1", headers=other_headers).status_code == 403

        monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET")
        assert client.get(f"/files/{fid}?inline=1", headers=headers).status_code == 401


@pytest.mark.parametrize("bound_project", [False, True])
def test_pin_session_file_without_export(local_project, monkeypatch, tmp_path, bound_project):
    from core.llm.tools import _paths
    from core.artifacts.store import get_artifact
    from core.services.local_grant_service import add_grant

    _, scope = local_project
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(root))
    source = root / "index.html"
    source.write_text("<h1>session output</h1>")
    pin = pin_tool(scope if bound_project else None, session_id="pin-chat")
    result = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    assert result["ok"], result
    assert len(result["pinned"]) == 1, result
    item = get_artifact(result["pinned"][0]["file_id"])
    assert Path(item["path"]).read_text() == "<h1>session output</h1>"
    assert source.exists()


@pytest.mark.parametrize("case", ["missing", "directory", "other-session", "symlink", "too-large", "cloud"])
def test_path_delivery_rejects_invalid_sources(local_project, monkeypatch, tmp_path, case):
    from dataclasses import replace
    from core.llm.tools import _paths
    from core.services.local_grant_service import add_grant
    from core.config.settings import settings
    import importlib

    _, scope = local_project
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(tmp_path))
    source = root / "output.txt"
    if case == "directory":
        source.mkdir()
    elif case in ("other-session", "symlink"):
        other = Path(_paths.workspace_directory("other-chat"))
        other.mkdir(parents=True)
        target = other / "private.txt"
        target.write_text("other conversation")
        if case == "symlink":
            source.symlink_to(target)
        else:
            source = target
    elif case == "too-large":
        source.write_text("oversized")
        monkeypatch.setattr(
            importlib.import_module("core.config.settings"), "settings",
            replace(settings, sandbox=replace(settings.sandbox, artifact_max_bytes=3)),
        )
    elif case == "cloud":
        source.write_text("cloud")
        monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    result = json.loads(asyncio.run(
        pin_tool(None, session_id="pin-chat")(file_paths=[str(source)])
    ).content[0].text)
    assert result["ok"] is False, result
    assert result["pinned"] == [] and len(result["failed"]) == 1
    assert workspace.get_pinned() == []


def test_session_relative_delivery_reports_partial_failure(local_project, monkeypatch, tmp_path):
    from core.llm.tools import _paths
    from core.services.local_grant_service import add_grant
    from core.artifacts.store import get_artifact

    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(root))
    source = root / "output.txt"
    source.write_text("delivered")
    result = json.loads(asyncio.run(
        pin_tool(None, session_id="pin-chat")(file_paths=["output.txt", "missing.txt"])
    ).content[0].text)
    assert result["ok"] is True and len(result["pinned"]) == 1
    assert len(result["failed"]) == 1
    source.unlink()
    item = get_artifact(result["pinned"][0]["file_id"])
    assert Path(item["path"]).read_text() == "delivered"


def test_session_delivery_deduplicates_retries_but_keeps_changed_content(local_project, monkeypatch, tmp_path):
    from core.llm.tools import _paths
    from core.services.local_grant_service import add_grant

    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(root))
    source = root / "output.txt"
    source.write_text("first")
    pin = pin_tool(None, session_id="pin-chat")
    first = json.loads(asyncio.run(pin(file_paths=[str(source), str(source)])).content[0].text)
    assert first["pinned_count"] == 1
    retry = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    assert retry["pinned"][0]["already_pinned"] is True
    assert retry["pinned_count"] == 1
    source.write_text("second")
    changed = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    assert changed["pinned_count"] == 2
    assert changed["pinned"][0]["file_id"] != first["pinned"][0]["file_id"]


def test_design_picker_accepts_local_images_without_pinning(local_project, monkeypatch, tmp_path):
    from core.llm.tools import _paths, _myspace_confirm
    from core.llm.tools.design_picker_tool import register_choose_design
    from core.services.local_grant_service import add_grant
    from core.llm.tool_permissions import (
        ToolPermissionRegistry, ToolPermissionService, PermissionRuntime, CURRENT_PERMISSION_TICKET,
    )
    from core.artifacts.store import get_artifact

    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(root))
    from PIL import Image
    options = []
    for name, color in [("a", "red"), ("b", "blue")]:
        source = root / (name + ".png")
        Image.new("RGB", (2, 2), color).save(source)
        options.append({"id": name, "title": name, "image_path": str(source)})
    async def choose(**kwargs):
        for option in kwargs["options"]:
            assert get_artifact(option["image_file_id"])["mime_type"] == "image/png"
        return {"status": "chosen", "option_id": "b"}
    monkeypatch.setattr(_myspace_confirm, "pick", choose)
    collector = ToolCollector()
    register_choose_design(collector, chat_id="pin-chat", user_id="pin-user")
    registry = ToolPermissionRegistry()
    registry.register("choose_design", collector.permission_specs["choose_design"], source="test")
    service = ToolPermissionService(registry, PermissionRuntime(
        chat_id="pin-chat", user_id="pin-user", interactive=False, approval_available=False,
    ))
    async def invoke():
        args = {"question": "Select", "options": options}
        outcome = await service.authorize(SimpleNamespace(name="choose_design", id="pick-call", input=args))
        assert outcome.proceed
        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            return await collector.get_tool("choose_design")._func(**args)
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)
    result = json.loads(asyncio.run(invoke()).content[0].text)
    assert result["ok"] and result["selected_id"] == "b"
    assert workspace.get_pinned() == []
