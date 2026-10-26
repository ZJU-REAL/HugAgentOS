import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from core.files.artifacts.store import get_artifact
from core.db.models import Artifact
from core.llm.runtime import workspace
from tests.llm.local_project_test_support import local_project, pin_tool


def session_file(tmp_path, monkeypatch):
    from core.sandbox.workspace import resolution as _paths
    from features.desktop.local.grants import add_grant

    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    root = Path(_paths.workspace_directory("pin-chat"))
    root.mkdir(parents=True)
    add_grant(str(root))
    source = root / "report.txt"
    source.write_text("first version")
    return source


def test_session_delivery_is_live_and_keeps_id_across_turns(local_project, db_session, tmp_path, monkeypatch):
    from api.routes.files import local_file_location, download_file
    from core.files.artifacts.reader import fetch_parsed_text
    from core.files.artifacts.service import persist_artifacts

    source = session_file(tmp_path, monkeypatch)
    pin = pin_tool(None, session_id="pin-chat")
    first = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    fid = first["pinned"][0]["file_id"]
    assert get_artifact(fid)["path"] == str(source)
    assert local_file_location(fid, SimpleNamespace(user_id="pin-user"), db_session)["path"] == str(source)
    workspace.init_state()
    source.write_text("second version, longer")
    second = json.loads(asyncio.run(pin(file_paths=[str(source)])).content[0].text)
    assert second["pinned"][0]["file_id"] == fid
    assert get_artifact(fid)["size"] == source.stat().st_size
    assert fetch_parsed_text(fid, "pin-user") == "second version, longer"
    response = download_file(fid, BackgroundTasks(), mode="direct", inline=True,
                             user=SimpleNamespace(user_id="pin-user"), db=db_session)
    assert response.path == str(source)
    persist_artifacts(db_session, "pin-user", "pin-chat", workspace.get_pinned())
    assert db_session.query(Artifact).count() == 0
    assert not list((tmp_path / "artifacts").glob("*.txt"))
    source.unlink()
    assert get_artifact(fid) is None
    with pytest.raises(HTTPException):
        local_file_location(fid, SimpleNamespace(user_id="pin-user"), db_session)


def test_session_canvas_saves_original_and_rechecks_ownership(local_project, db_session, tmp_path, monkeypatch):
    from api.routes.v1.file_upload import overwrite_file

    source = session_file(tmp_path, monkeypatch)
    result = json.loads(asyncio.run(pin_tool(None, "pin-chat")(file_paths=[str(source)])).content[0].text)
    fid = result["pinned"][0]["file_id"]
    response = overwrite_file(
        fid, UploadFile(filename="report.txt", file=io.BytesIO(b"saved in original")),
        SimpleNamespace(user_id="pin-user"), db_session,
    )
    assert response["file_id"] == fid
    assert source.read_text() == "saved in original"
    with pytest.raises(HTTPException):
        overwrite_file(
            fid, UploadFile(filename="report.txt", file=io.BytesIO(b"unauthorized")),
            SimpleNamespace(user_id="other-user"), db_session,
        )


@pytest.mark.parametrize("change", ["symlink", "revoked"])
def test_session_access_revalidates_source(local_project, db_session, tmp_path, monkeypatch, change):
    from api.routes.files import local_file_location
    from features.desktop.local.grants import remove_grant
    from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET

    source = session_file(tmp_path, monkeypatch)
    result = json.loads(asyncio.run(pin_tool(None, "pin-chat")(file_paths=[str(source)])).content[0].text)
    fid = result["pinned"][0]["file_id"]
    if change == "symlink":
        private = tmp_path / "private.txt"
        private.write_text("private data")
        source.unlink()
        source.symlink_to(private)
    else:
        remove_grant(str(source.parent))
    token = CURRENT_PERMISSION_TICKET.set(None)
    try:
        with pytest.raises(HTTPException):
            local_file_location(fid, SimpleNamespace(user_id="pin-user"), db_session)
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)


def test_write_and_pin_use_same_session_reference(local_project, tmp_path, monkeypatch):
    from core.llm.tools.write_tool import register_write
    from core.llm.factory.tools.definitions import ToolAssembly
    from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET, PermissionTicket, PermissionIntent

    source = session_file(tmp_path, monkeypatch)
    source.unlink()
    collector = ToolAssembly()
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: SimpleNamespace(name="script_runner"))
    register_write(collector, chat_id="pin-chat", user_id="pin-user")
    token = CURRENT_PERMISSION_TICKET.set(PermissionTicket(
        "Write", "write", "", "test", (PermissionIntent("local_path", "write", str(source), "create"),),
    ))
    try:
        written = json.loads(asyncio.run(next(t for t in collector if t.name == "Write")._func(
            file_path=str(source), content="written original", register_as_artifact=True,
        )).content[0].text)
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)
    assert written["ok"], written
    assert get_artifact(written["file_id"])["path"] == str(source)
    pinned = json.loads(asyncio.run(pin_tool(None, "pin-chat")(file_paths=[str(source)])).content[0].text)
    assert pinned["pinned"][0]["file_id"] == written["file_id"]
    assert pinned["pinned"][0]["already_pinned"]
    assert not list((tmp_path / "artifacts").glob("*.txt"))
