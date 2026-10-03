"""Local project delivery uses the original file through the public tools/API."""

import asyncio

import json

from pathlib import Path

from types import SimpleNamespace


import core.db.models  # noqa: F401 - register model metadata

from core.llm import workspace

from core.llm.tool_collector import ToolCollector


from tests.llm.local_project_test_support import local_project, pin_tool


def test_session_delivery_deduplicates_and_rejects_conflicting_names(
    local_project, monkeypatch, tmp_path
):
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
    assert changed["ok"] is False
    assert changed["pinned_count"] == 1
    assert "同名文件" in changed["failed"][0]["error"]
    renamed = source.with_name("output-v2.txt")
    source.rename(renamed)
    delivered = json.loads(asyncio.run(pin(file_paths=[str(renamed)])).content[0].text)
    assert delivered["pinned_count"] == 2
    assert delivered["pinned"][0]["file_id"] != first["pinned"][0]["file_id"]


def test_design_picker_accepts_local_images_without_pinning(local_project, monkeypatch, tmp_path):
    from core.llm.tools import _paths, _myspace_confirm
    from core.llm.tools.design_picker_tool import register_choose_design
    from core.services.local_grant_service import add_grant
    from core.llm.tool_permissions import (
        ToolPermissionRegistry,
        ToolPermissionService,
        PermissionRuntime,
        CURRENT_PERMISSION_TICKET,
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
            return await collector.get_tool("choose_design")._func(**args)
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)

    result = json.loads(asyncio.run(invoke()).content[0].text)
    assert result["ok"] and result["selected_id"] == "b"
    assert workspace.get_pinned() == []
