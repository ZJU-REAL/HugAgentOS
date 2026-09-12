"""Desktop capability failures must not prevent an ordinary answer."""

import pytest
from core.capabilities import runtime, skills
from core.services.desktop_capability_protocol import skill_content_hash


def test_full_factory_can_reply_when_selected_plugin_has_no_tools(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import asyncio
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
import core.db.models
from core.db.engine import Base, engine
from core.llm import agent_factory
Base.metadata.create_all(engine)

class EmptyLoader:
    def load_all_metadata(self): return {}
    def get_skill_dir(self, *args): return None
    def register_skills_to_toolkit(self, *args, **kwargs): return 0

class Model:
    model, context_size = "offline-test", 32768
    calls = 0
    async def __call__(self, messages, tools=None, **kwargs):
        self.calls += 1
        return ChatResponse(content=[TextBlock(text="Plugin unavailable; we can continue.")],
            is_last=True, usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01))
    async def count_tokens(self, *args, **kwargs): return 1

agent_factory.get_skill_loader = lambda: EmptyLoader()
async def main():
    agent, clients = await agent_factory.create_agent_executor(
        disable_tools=True, run_id="empty-plugin-answer", max_iters=1,
        required_plugin_id="empty", required_plugin_name="UnavailableTestPlugin")
    assert "UnavailableTestPlugin" in agent._jx_compaction_system_prompt
    model = Model()
    agent.model = model
    agent.state.model_pinned = True
    await agent.reply(UserMsg(name="user", content="Please continue"))
    assert model.calls == 1
    for client in clients:
        await client.close()
asyncio.run(main())
"""
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'factory.db'}",
        "REDIS_URL": "",
        "SANDBOX_TOOLS_ENABLED": "false",
        "HUGAGENT_CAPS_ROOT": str(tmp_path / "caps"),
    }
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.fixture
def local_skill(index_db, caps_root, monkeypatch):
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)

    def publish(name):
        md = f"---\nname: {name}\ndescription: Test skill\n---\nOriginal instructions"
        return skills.publish_local_skill(
            name,
            files={"SKILL.md": md},
            content_hash=skill_content_hash(md, {}),
            owner_user_id="owner",
        )

    return publish


def test_execution_outputs_and_source_edits_do_not_break_other_runs(local_skill):
    source = local_skill("news")
    run = runtime.prepare("first", "owner", skill_ids=["news"], allow_unavailable=True)
    (run.view_dir / "news" / "query.json").write_text("{}")
    assert not (source.path / "query.json").exists()
    source.entry_file.write_text(source.entry_file.read_text().replace("Original", "Updated"))
    runtime.validate(run)
    assert "Original" in (run.view_dir / "news" / "SKILL.md").read_text()
    following = runtime.prepare("second", "owner", skill_ids=["news"], allow_unavailable=True)
    assert "Updated" in (following.view_dir / "news" / "SKILL.md").read_text()


def test_missing_skill_and_unselected_broken_skill_do_not_block(local_skill):
    local_skill("good")
    bad = local_skill("bad")
    bad.entry_file.unlink()
    run = runtime.prepare("missing", "owner", skill_ids=["good", "absent"], allow_unavailable=True)
    assert set(run.bindings) == {"good"}
    assert "absent" in run.unavailable
    runtime.validate(run)


def test_execution_copy_keeps_executable_scripts(local_skill):
    import os

    if os.name == "nt":
        pytest.skip("POSIX executable bits")
    source = local_skill("executable")
    script = source.path / "run.sh"
    script.write_text("#!/bin/sh\necho ready\n")
    script.chmod(0o755)
    run = runtime.prepare("executable", "owner", skill_ids=["executable"], allow_unavailable=True)
    assert os.access(run.view_dir / "executable" / "run.sh", os.X_OK)


def test_disabled_skill_disappears_without_blocking_other_skills(local_skill):
    from core.capabilities import registry

    local_skill("good")
    local_skill("disabled")
    run = runtime.prepare(
        "disabled", "owner", skill_ids=["good", "disabled"], allow_unavailable=True
    )
    registry.set_enabled("skill:local:disabled", False)
    runtime.view_for_execution(run.run_id, "owner")
    assert (run.view_dir / "good" / "SKILL.md").is_file()
    assert not (run.view_dir / "disabled").exists()


def test_foreign_directory_is_preserved_but_not_exposed(local_skill):
    from core.capabilities import registry

    local_skill("good")
    local_skill("disabled")
    run = runtime.prepare(
        "foreign", "owner", skill_ids=["good", "disabled"], allow_unavailable=True
    )
    foreign = run.view_dir / "disabled"
    foreign.unlink()
    foreign.mkdir()
    (foreign / "private.txt").write_text("keep")
    registry.set_enabled("skill:local:disabled", False)
    actual = runtime.view_for_execution(run.run_id, "owner")
    assert actual != run.view_dir
    assert not (actual / "disabled").exists()
    assert (foreign / "private.txt").read_text() == "keep"
    assert (actual / "good" / "SKILL.md").is_file()


def test_edited_required_dependency_is_included_and_usable(local_skill):
    parent = local_skill("parent")
    child = local_skill("child")
    parent.entry_file.write_text(
        parent.entry_file.read_text().replace(
            "description: Test skill",
            "description: Test skill\ndependencies:\n  - kind: skill\n    id: child",
        )
    )
    child.entry_file.write_text(child.entry_file.read_text().replace("Original", "Updated"))
    run = runtime.prepare("dependencies", "owner", skill_ids=["parent"], allow_unavailable=True)
    assert set(run.bindings) == {"parent", "child"}
    checked = runtime.preflight(run, skill_ids=["parent"], available_models=set())
    assert not checked.unavailable
    assert "Updated" in (checked.view_dir / "child" / "SKILL.md").read_text()


@pytest.mark.asyncio
async def test_revoked_skill_returns_tool_error_and_other_skill_stays_readable(local_skill):
    from core.capabilities import registry
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.skill_tool import register_sandboxed_view_text_file

    local_skill("good")
    local_skill("disabled")
    run = runtime.prepare("reader", "owner", skill_ids=["good", "disabled"], allow_unavailable=True)
    loader = runtime.frozen_loader(run)
    collector = ToolCollector()
    register_sandboxed_view_text_file(
        collector, [str(run.view_dir / n) for n in run.bindings], loader
    )
    tool = next(t for t in collector.function_tools if t.name == "view_text_file")
    registry.set_enabled("skill:local:disabled", False)
    bad = await tool(file_path="/workspace/skills/disabled/SKILL.md")
    assert "Original instructions" not in str(bad)
    good = await tool(file_path="/workspace/skills/good/SKILL.md")
    assert "Original instructions" in str(good)


def test_connector_update_excludes_only_changed_binding(local_skill):
    run = runtime.prepare("connector-update", "owner", skill_ids=[], allow_unavailable=True)
    first = {"one": {"command": "old"}, "two": {"command": "stable"}}
    runtime.bind_mcp(run, first, None)
    changed = runtime.bind_mcp(run, {"one": {"command": "new"}, "two": first["two"]}, None)
    assert set(changed) == {"two"}
    assert runtime.get(run.run_id).unavailable["mcp:one"] == "connector_changed"
    runtime.rebuild(run)
    assert runtime.get(run.run_id).mcp_frozen
    assert runtime.get(run.run_id).unavailable["mcp:one"] == "connector_changed"


def test_legacy_changed_run_recovers_without_replaying_changed_skill(local_skill):
    local_skill("good")
    changed = local_skill("changed")
    runtime.prepare("legacy", "owner", skill_ids=["good", "changed"])
    changed.entry_file.write_text("Changed after interruption")
    resumed = runtime.prepare(
        "legacy", "owner", skill_ids=["good", "changed"], allow_unavailable=True
    )
    assert resumed.allow_unavailable
    assert resumed.unavailable["changed"] == "legacy_revision_unavailable"
    assert (resumed.view_dir / "good" / "SKILL.md").is_file()
    assert not (resumed.view_dir / "changed").exists()


@pytest.mark.asyncio
async def test_connector_disabled_midrun_never_calls_underlying_tool(local_skill, monkeypatch):
    from agentscope.tool import ToolBase, ToolChunk, Toolkit
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.message import TextBlock
    from core.llm.capability_tools import AvailableMCPClient
    from types import SimpleNamespace
    from dataclasses import replace
    from core.capabilities import mcp_json

    calls = []

    class Tool(ToolBase):
        name, description, input_schema = "lookup", "Lookup", {"type": "object", "properties": {}}
        is_concurrency_safe, is_read_only, is_mcp, mcp_name = True, True, True, "one"

        async def check_permissions(self, *a, **kw):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW)

        async def __call__(self, **kw):
            calls.append("called")
            return ToolChunk(content=[TextBlock(text="result")])

    async def list_tools():
        return [Tool()]

    run = runtime.prepare("connector-disabled", "owner", skill_ids=[], allow_unavailable=True)
    run = replace(
        run,
        mcp_bindings={"one": {"install_id": "mcp:local-json:one", "authorization_checked": True}},
    )
    client = AvailableMCPClient(
        SimpleNamespace(name="one", list_tools=list_tools, is_stateful=False), run
    )
    toolkit = Toolkit(mcps=[client])
    assert (await toolkit.get_tool_schemas())[0]["function"]["name"] == "lookup"
    monkeypatch.setattr(mcp_json, "local_server_configs", lambda: {})
    result = await (await client.list_tools())[0]()
    assert not calls
    assert "result" not in "".join(block.text for block in result.content)
