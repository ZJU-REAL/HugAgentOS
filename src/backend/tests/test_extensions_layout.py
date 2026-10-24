"""Distribution contracts for bundled extensions and platform agent defaults."""
from core.llm import prompt_files as prompt_files_module
from core.llm import prompt_rendering as prompt_rendering_module

import importlib.util
import json
from pathlib import Path


def test_extension_roots_are_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from extensions.paths import (
        AGENTS_DIR,
        EXTENSIONS_DIR,
        MCP_SERVERS_DIR,
        PLUGINS_DIR,
        SKILLS_DIR,
    )

    assert EXTENSIONS_DIR == Path(__file__).resolve().parents[1] / "extensions"
    for directory in (SKILLS_DIR, AGENTS_DIR, PLUGINS_DIR, MCP_SERVERS_DIR):
        assert directory.is_dir()
    from core.capabilities.skills.config import get_default_skill_sources
    from core.capabilities.plugins.packaging.definitions import PLUGIN_BUNDLES_DIR
    from features.marketplace.agents_state import AGENT_MARKET_DIR
    from features.marketplace.skills_state import MARKETPLACE_DIR

    assert get_default_skill_sources()[0].root_dir == SKILLS_DIR / "default"
    assert PLUGIN_BUNDLES_DIR == PLUGINS_DIR
    assert AGENT_MARKET_DIR == AGENTS_DIR / "marketplace"
    assert MARKETPLACE_DIR == SKILLS_DIR / "marketplace"


def test_builtin_agent_packages_preserve_identity_and_prompt_source(monkeypatch):
    from core.capabilities.agents.builtin import BUILTIN_SUBAGENTS, load_builtin_subagent_prompt
    from core.llm import prompt_versions as pvs
    from extensions.paths import DEFAULT_AGENTS_DIR

    monkeypatch.setattr(model_services.registration_state(), 'prompt_renderer', lambda *args: None)
    for spec in BUILTIN_SUBAGENTS:
        package = DEFAULT_AGENTS_DIR / spec.role
        manifest = json.loads((package / "agent.json").read_text(encoding="utf-8"))
        assert spec.agent_id == "builtin." + manifest["role"]
        assert spec.name == manifest["name"]
        assert spec.max_iters == manifest["max_iters"]
        assert (
            load_builtin_subagent_prompt(spec)
            == (package / "system.md").read_text(encoding="utf-8").strip()
        )
    assert prompt_files_module.fs_dir("subagents") == DEFAULT_AGENTS_DIR
    parts = {part["part_id"]: part["content"].strip() for part in prompt_files_module.read_fs_parts("subagents")}
    assert parts == {spec.role: load_builtin_subagent_prompt(spec) for spec in BUILTIN_SUBAGENTS}


def test_builtin_agent_prompt_still_prefers_active_database_version(monkeypatch):
    from core.capabilities.agents.builtin import get_builtin_subagent, load_builtin_subagent_prompt
    from core.llm import prompt_versions as pvs

    monkeypatch.setattr(model_services.registration_state(), 'prompt_renderer', lambda kind, role: "active " + role)
    assert load_builtin_subagent_prompt(get_builtin_subagent("builtin.worker")) == "active worker"


def test_mcp_registry_loads_new_package_and_old_module_is_absent():
    from extensions.mcp_servers.registry import SERVERS
    from extensions.paths import MCP_SERVERS_DIR

    for spec in SERVERS.values():
        module = importlib.util.find_spec("extensions.mcp_servers." + spec.package + ".server")
        assert module is not None
        assert Path(module.origin).is_relative_to(MCP_SERVERS_DIR)
    assert importlib.util.find_spec("mcp_servers") is None

from core.harness.registry import model_services
