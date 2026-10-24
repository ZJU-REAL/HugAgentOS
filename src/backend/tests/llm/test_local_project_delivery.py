"""Local project delivery uses the original file through the public tools/API."""

import asyncio

import json

from pathlib import Path

from types import SimpleNamespace


import core.db.models  # noqa: F401 - register model metadata

from core.llm.runtime import workspace

from core.llm.factory.tools.definitions import ToolAssembly


from tests.llm.local_project_test_support import local_project, pin_tool


def test_session_delivery_deduplicates_live_references_and_tracks_renames(
    local_project, monkeypatch, tmp_path
):
    from core.sandbox.workspace import resolution as _paths
    from features.desktop.local.grants import add_grant

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
    assert changed["ok"] is True
    assert changed["pinned_count"] == 1
    assert changed["pinned"][0]["file_id"] == first["pinned"][0]["file_id"]
    assert changed["pinned"][0]["already_pinned"] is True
    from core.files.artifacts.local_reference import authorized_reference
    reference = authorized_reference(first["pinned"][0]["file_id"], "pin-user")
    assert reference["path"] == str(source)
    assert Path(reference["path"]).read_text() == "second"
    assert authorized_reference(reference["file_id"], "another-user") is None
    renamed = source.with_name("output-v2.txt")
    source.rename(renamed)
    delivered = json.loads(asyncio.run(pin(file_paths=[str(renamed)])).content[0].text)
    assert delivered["pinned_count"] == 2
    assert delivered["pinned"][0]["file_id"] != first["pinned"][0]["file_id"]
    assert authorized_reference(first["pinned"][0]["file_id"], "pin-user") is None
    assert Path(authorized_reference(delivered["pinned"][0]["file_id"], "pin-user")["path"]).read_text() == "second"


def test_design_picker_accepts_local_images_without_pinning(local_project, monkeypatch, tmp_path):
    from core.sandbox.workspace import resolution as _paths
    from core.llm.tools import confirmation
    from features.sites.tools.design_picker import register_choose_design
    from features.desktop.local.grants import add_grant
    from core.sandbox.permissions.tool_specs import ToolPermissionRegistry
    from core.llm.tool_permissions import ToolPermissionService
    from core.sandbox.permissions.dispatch_models import PermissionRuntime, CURRENT_PERMISSION_TICKET
    from core.files.artifacts.store import get_artifact

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

    monkeypatch.setattr(confirmation, "pick", choose)
    collector = ToolAssembly()
    register_choose_design(collector, chat_id="pin-chat", user_id="pin-user")
    registry = ToolPermissionRegistry()
    registry.register("choose_design", collector.permission_specs["choose_design"], source="test")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="pin-chat",
            user_id="pin-user",
            interactive=False,
            approval_available=False,
        ),
    )

    async def invoke():
        args = {"question": "Select", "options": options}
        outcome = await service.authorize(
            SimpleNamespace(name="choose_design", id="pick-call", input=args)
        )
        assert outcome.proceed
        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            return await next(t for t in collector if t.name == "choose_design")._func(**args)
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)

    result = json.loads(asyncio.run(invoke()).content[0].text)
    assert result["ok"] and result["selected_id"] == "b"
    assert workspace.get_pinned() == []


def test_expired_inherited_ticket_cannot_read_or_overwrite_reference(local_project, db_session):
    from contextvars import copy_context
    from fastapi import HTTPException
    import pytest
    from core.files.artifacts.local_reference import authorized_reference, overwrite_reference
    from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET, PermissionTicket, PermissionIntent
    from features.desktop.local.grants import remove_grant

    source, scope = local_project
    delivered = json.loads(asyncio.run(pin_tool(scope)(file_paths=[str(source)])).content[0].text)
    fid = delivered["pinned"][0]["file_id"]
    ticket = PermissionTicket("Write", "finished-call", "", "test-write", (
        PermissionIntent("local_path", "write", str(source), "temporary approval"),
    ))
    token = CURRENT_PERMISSION_TICKET.set(ticket)
    inherited = copy_context()
    CURRENT_PERMISSION_TICKET.reset(token)
    remove_grant(str(source.parent))
    # An in-flight approval is valid only until the dispatch completes.
    assert inherited.run(authorized_reference, fid, "pin-user") is not None
    ticket.lifetime.expire()
    assert inherited.run(authorized_reference, fid, "pin-user") is None
    with pytest.raises(HTTPException):
        inherited.run(overwrite_reference, fid, "pin-user", b"unauthorized", db_session)
    assert source.read_text() == "original report"
