"""A task's plugins: the 插件目录 lists what is synced and still granted; load_plugin connects one."""
import json
from types import SimpleNamespace

import pytest

from core.llm.factory.tools.definitions import ToolAssembly
from personal_agent.identity.capabilities import platform_synced, task_mcp_ids, task_plugins
from personal_agent.tools import plugins as module
from tests.personal_agent_capability_files import manifest, skill, workspace

GRANTS = {"skill_ids": [], "mcp_ids": ["web_fetch"], "plugins": [
    {"id": "notes@owner", "name": "Notes", "skill_ids": ["notes-write"], "mcp_ids": []},
    {"id": "crm@owner", "name": "CRM", "skill_ids": ["crm-lookup"], "mcp_ids": ["crm_mcp"]}]}


@pytest.fixture
def ws(monkeypatch, tmp_path):
    root = workspace(monkeypatch, tmp_path)
    skill(root, "plugins/notes/skills/notes-write", "Write meeting notes")
    skill(root, "plugins/notes/skills/notes-revoked", "No longer granted")
    skill(root, "plugins/crm/skills/crm-lookup", "Look up a customer")
    skill(root, "plugins/gone/skills/g", "Uninstalled")
    manifest(root, "plugins", {
        "notes": {"install_id": "notes@owner", "name": "Notes", "description": "Meeting notes", "hash": "1",
                  "skills": {"notes-write": "skills/notes-write", "notes-revoked": "skills/notes-revoked"}, "mcp_ids": []},
        "crm": {"install_id": "crm@owner", "name": "CRM", "description": "Customer records", "hash": "2",
                "skills": {"crm-lookup": "skills/crm-lookup"}, "mcp_ids": ["crm_mcp", "crm_revoked"]},
        # Synced, but the user no longer grants it.
        "gone": {"install_id": "gone@owner", "name": "Gone", "hash": "3", "skills": {"g": "skills/g"}, "mcp_ids": []}})
    return root


def test_directory_lists_synced_plugins_still_granted(ws):
    from personal_agent.workspace import prompting
    listed = task_plugins("pa_x", GRANTS)
    assert [(p.slug, p.install_id, p.skill_ids, p.mcp_ids) for p in listed] == [
        ("crm", "crm@owner", ["crm-lookup"], ["crm_mcp"]), ("notes", "notes@owner", ["notes-write"], [])]
    text = prompting.render(prompting.sections("pa_x", grants=GRANTS, seed_missing=False))
    assert "## 插件目录" in text and "- `crm`（CRM）：Customer records" in text and "- `notes`（Notes）：Meeting notes" in text
    assert "`gone`" not in text
    # No plugin component connects when a task starts, only those of plugins it already loaded.
    assert task_mcp_ids(GRANTS) == ["web_fetch"]
    assert task_mcp_ids(GRANTS, [listed[0]]) == ["web_fetch", "crm_mcp"]
    # Revoked: the plugin leaves the directory, whatever is still on disk.
    assert task_plugins("pa_x", {**GRANTS, "plugins": []}) == []


def test_a_malformed_manifest_lists_nothing(ws):
    (ws / "plugins" / ".synced.json").write_text(json.dumps({"version": 2, "plugins": {}}))
    assert task_plugins("pa_x", GRANTS) == []
    (ws / "plugins" / ".synced.json").write_text("{not json")
    assert task_plugins("pa_x", GRANTS) == []


def test_platform_synced_files_are_read_only(ws):
    manifest(ws, "skills", {"docx": {"source": "selected", "hash": "1"}})
    assert platform_synced("pa_x", "skills/docx/SKILL.md") and platform_synced("pa_x", "skills/.synced.json")
    assert platform_synced("pa_x", "plugins/notes/skills/notes-write/SKILL.md")
    # The agent's own skills, and copies it makes of platform ones, are its to edit.
    assert not platform_synced("pa_x", "skills/my-docx/SKILL.md") and not platform_synced("pa_x", "work/t1/a.md")


class Live:
    """The run's real toolkit, as the factory hands it to the activation holder."""

    def __init__(self):
        self.tool_groups = [SimpleNamespace(tools=[SimpleNamespace(name="pa_read_file")], skills_or_loaders=[])]
        self.invalidated = 0

    def invalidate_execution_surface(self):
        self.invalidated += 1


@pytest.fixture
def registered(ws, monkeypatch):
    recorded, history = [], []
    monkeypatch.setattr(module, "owned_task", lambda task_id, uid: {"agent_id": "pa_x"})
    monkeypatch.setattr("core.capabilities.plugins.runtime.activation_history.load_activated_plugin_slugs",
                        lambda chat_id: list(history))
    monkeypatch.setattr("core.capabilities.plugins.runtime.activation_history.record_plugin_activation",
                        lambda chat_id, ids, **kw: recorded.append((chat_id, list(ids))))

    def register():
        toolkit = ToolAssembly()
        request = SimpleNamespace(run_id="run_1", chat_id="task_chat", enabled_mcp_ids=["web_fetch"])
        module.register_load_plugin(toolkit, request, "pa_x", GRANTS, "t1", "owner")
        # What the factory does once the agent exists.
        toolkit.plugin_runtime.update(toolkit=Live(), permission_context=SimpleNamespace(allow_rules={}), close_list=[])
        return toolkit, {tool.name: tool._func for tool in toolkit}
    return SimpleNamespace(register=register, recorded=recorded, history=history)


async def test_load_plugin_returns_its_skill_paths_for_this_task(registered):
    toolkit, tools = registered.register()
    answer = json.loads((await tools["load_plugin"]("notes")).content[0].text)
    assert answer["plugin"] == "notes" and answer["tools"] == []
    assert answer["skills"] == [{"id": "notes-write", "path": "/workspace/plugins/notes/skills/notes-write/SKILL.md",
                                 "description": "Write meeting notes"}]
    # Recorded on the task's own chat session: a later run of this task connects it again.
    assert registered.recorded == [("task_chat", ["notes@owner"])]
    assert toolkit.plugin_runtime["toolkit"].invalidated == 1
    # Loading it again only repeats the paths.
    again = json.loads((await tools["load_plugin"]("Notes")).content[0].text)
    assert again["skills"] == answer["skills"] and len(registered.recorded) == 1
    unknown = json.loads((await tools["load_plugin"]("gone")).content[0].text)
    assert "gone" in unknown["error"] and "crm" in unknown["error"]


async def test_load_plugin_connects_its_mcp_servers(registered, monkeypatch):
    connected = []

    class Client:
        tools = [SimpleNamespace(name="crm_search")]

    async def discover(key, cfg, **kw):
        connected.append(key)
        return Client()

    async def ensure(cfgs):
        return None
    service = SimpleNamespace(get_all_servers=lambda enabled_only=True: {"crm_mcp": {"url": "http://crm"}, "crm_revoked": {}},
                              get_owned_servers=lambda uid, enabled_only=False: {})
    monkeypatch.setattr("core.capabilities.mcp.service.McpServerConfigService.get_instance", lambda: service)
    monkeypatch.setattr("core.llm.mcp.lifetime.discover", discover)
    monkeypatch.setattr("core.harness.registry.local_publication.ensure_native_servers", ensure)
    monkeypatch.setattr("core.llm.factory.tools.mcp_config.inject_runtime_headers", lambda cfgs, **kw: cfgs)
    toolkit, tools = registered.register()
    answer = json.loads((await tools["load_plugin"]("crm")).content[0].text)
    # Only the server the user still grants.
    assert connected == ["crm_mcp"] and answer["tools"] == ["crm_search"]
    assert answer["skills"][0]["path"] == "/workspace/plugins/crm/skills/crm-lookup/SKILL.md"
    runtime = toolkit.plugin_runtime
    assert [t.name for t in runtime["toolkit"].tool_groups[0].tools] == ["pa_read_file", "crm_search"]
    assert "crm_search" in runtime["permission_context"].allow_rules and len(runtime["close_list"]) == 1


async def test_a_later_run_of_the_task_starts_with_the_plugin_loaded(registered):
    registered.history.append("crm@owner")
    assert [p.slug for p in module.loaded_plugins("pa_x", GRANTS, "task_chat")] == ["crm"]
    toolkit, tools = registered.register()
    assert toolkit.plugin_runtime["activated_slugs"] == {"crm"}
    answer = json.loads((await tools["load_plugin"]("crm")).content[0].text)
    assert answer["plugin"] == "crm" and answer["skills"][0]["id"] == "crm-lookup" and registered.recorded == []


def test_no_plugins_no_tool(ws):
    toolkit = ToolAssembly()
    request = SimpleNamespace(run_id="run_1", chat_id="task_chat", enabled_mcp_ids=[])
    module.register_load_plugin(toolkit, request, "pa_x", {**GRANTS, "plugins": []}, "t1", "owner")
    assert list(toolkit) == [] and getattr(toolkit, "plugin_runtime", None) is None
