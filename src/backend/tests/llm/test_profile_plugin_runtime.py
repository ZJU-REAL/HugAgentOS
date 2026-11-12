"""A profile agent's load_plugin holder reaches the factory; the sub-agent path keeps its own."""
from types import SimpleNamespace

import pytest

from core.llm.factory.prompts import system
from core.llm.factory.tools.definitions import ToolAssembly


def _kwargs(toolkit, plugin_runtime=None):
    manifest = SimpleNamespace(add_prompt_section=lambda *a, **kw: None)
    return dict(_agent_ref=None, _elapsed=lambda: "", _log=SimpleNamespace(info=lambda *a, **kw: None),
                _manifest_builder=manifest, _ontology_hidden_tools=set(), _ontology_runtime={},
                _plan_tool_enabled=False, _plugin_runtime=plugin_runtime, _progressive=None, _sbx_sess=None,
                _subagent_progressive=None, _tool_approval_available=False, _turbo_code_exec=False, cfg=None,
                enabled_mcp_keys=[], kb_catalog_section="", loader=None, skill_ids_to_register=[],
                tool_schemas=[], toolkit=toolkit)


def test_a_collector_starts_without_a_holder():
    assert ToolAssembly().plugin_runtime is None


@pytest.mark.asyncio
async def test_profile_hands_its_holder_to_the_factory(monkeypatch):
    async def prompt(request):
        return "profile prompt"
    monkeypatch.setattr("core.harness.registry.agent_profile", lambda name: SimpleNamespace(system_prompt=prompt))
    request = SimpleNamespace(profile="personal_agent")
    toolkit = ToolAssembly()
    # Without a holder the profile has no load_plugin, as before.
    assert (await system.system_prompt(request, **_kwargs(toolkit)))[2:] == (None, "profile prompt")
    toolkit.plugin_runtime = holder = {"toolkit": None}
    assert (await system.system_prompt(request, **_kwargs(toolkit)))[2] is holder


@pytest.mark.asyncio
async def test_subagent_path_ignores_a_collector_holder(monkeypatch):
    monkeypatch.setattr(system, "build_subagent_system_prompt", lambda *a, **kw: "sub prompt")
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    request = SimpleNamespace(profile=None, user_agent=SimpleNamespace(agent_id="a1", updated_at="1"))
    toolkit = ToolAssembly()
    toolkit.plugin_runtime = {"toolkit": None}
    own = {"from": "factory"}
    result = await system.system_prompt(request, **_kwargs(toolkit, plugin_runtime=own))
    assert result[2] is own and result[3].startswith("sub prompt")
