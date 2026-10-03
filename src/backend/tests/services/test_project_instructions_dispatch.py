"""Project-instruction dispatch and tools."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from api.schemas import ChatRequest
from core.services.project_instructions import ProjectInstructionsService
from core.services.project_scope import build_project_ctx
from core.services.project_service import ProjectService
from tests.services.project_instructions_support import env, local, update


def test_chat_dispatch_expands_command_and_loads_fresh_rules(env):
    from api.routes.v1.chats.agent_targets import _resolve_chat_agent_targets
    from api.routes.v1.chats.request_context import _build_ctx

    db, _ = env
    p = ProjectService(db).create_personal("alice", "Dispatch")
    req = ChatRequest(
        chat_id="new",
        message="/初始化指令",
        project_id=p.project_id,
        mode_slug="turbo",
        chat_mode="turbo",
    )
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
    result = ProjectService(db).update(
        p.project_id,
        "alice",
        {
            "instructions": "New instructions",
            "instructions_revision": rev,
            "name": "Renamed",
            "description": "New description",
            "memory_enabled": False,
        },
        level="admin",
    )
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
        collector,
        project_id=p.project_id,
        user_id="alice",
        allow_write=False,
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
    from core.llm.tools._state import ReadStateTracker
    from core.llm.tools.read_tool import register_read
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
    register_read(
        collector,
        chat_id="read-init-test",
        user_id="alice",
        state=ReadStateTracker(),
        project_folder_name=scope.folder_name,
        scope=scope,
    )
    read = collector.get_tool("Read")._func
    root = f"/myspace/{scope.folder_name}/AGENTS.md"
    result = json.loads((await read(root)).content[0].text)
    assert "FRESH-628" in result["content"] and "STALE" not in result["content"]
    assert list(cache.values()) == [b"FRESH-628"]
    nested = json.loads(
        (await read(f"/myspace/{scope.folder_name}/child/AGENTS.md")).content[0].text
    )
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
