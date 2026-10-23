"""Owner selections persist and the separate browser cannot enter Personal Agent."""
import pytest
from tests.test_personal_agent import sessions, personal_agent_on
from personal_agent.context import AgentContext
from personal_agent.identity.capabilities import capability_ids

@pytest.fixture
def available(monkeypatch):
    permitted = {"skills": ["officecli-docx", "custom-skill", "plugin-skill", "browser-automation-browser-automation-u"],
                 "mcps": ["internet_search", "web_fetch", "custom-tool", "plugin-tool", "browser-automation-browser_runtime-u"]}
    plugins = [{"install_id": "browser-automation@owner", "slug": "browser-automation",
                "enabled": True, "skills": ["browser-automation-browser-automation-u"],
                "mcp": ["browser-automation-browser_runtime-u"]},
               {"install_id": "sample@owner", "slug": "sample", "name": "Sample", "enabled": True,
                "skills": ["plugin-skill"], "mcp": ["plugin-tool"]}]
    monkeypatch.setattr('core.capabilities.catalog.resolver.resolve_all_runtime_enabled',
                        lambda db, uid: (permitted["skills"], [], permitted["mcps"]))
    monkeypatch.setattr('core.capabilities.plugins.management.list_installed', lambda db, **kw: plugins)
    monkeypatch.setattr('features.desktop.capabilities.device_catalog.plugin_entries', lambda user_id=None: [])
    return permitted, plugins

def test_browser_excluded_from_defaults_and_stale_runtime_config(sessions, available):
    with sessions() as db:
        config = AgentContext(db, "owner").identity.ensure()["config"]
        assert [p["id"] for p in config["plugins"]] == ["sample@owner"]
    stale = {"skill_ids": ["browser-automation-browser-automation-u"],
             "mcp_ids": ["browser_runtime"],
             "plugins": [{"id": "browser-automation@owner", "skill_ids": [], "mcp_ids": ["browser-automation-browser_runtime-u"]}]}
    assert capability_ids(stale) == ([], [])

def test_selection_survives_reopen_and_new_plugin(sessions, available):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        svc.identity.configure({"skill_ids": [], "mcp_ids": [], "plugin_ids": []})
        available[1].append({"install_id": "new@owner", "slug": "new", "enabled": True,
                             "skills": ["plugin-skill"], "mcp": []})
        config = AgentContext(db, "owner").identity.ensure()["config"]
        assert capability_ids(config) == ([], [])
        config = svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": ["web_fetch"], "plugin_ids": ["sample@owner"]})["config"]
        assert capability_ids(config) == (["custom-skill", "plugin-skill"], ["web_fetch", "plugin-tool"])

def test_invalid_selection_is_atomic_and_revocation_removes_execution(sessions, available):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": [], "plugin_ids": []})
        for patch in ({"skill_ids": ["foreign"]}, {"plugin_ids": ["browser-automation@owner"]},
                      {"mcp_ids": ["browser-automation-browser_runtime-u"]}, {"skill_ids": "custom-skill"}):
            with pytest.raises(ValueError):
                svc.identity.configure(patch)
        assert svc.identity.ensure()["config"]["skill_ids"] == ["custom-skill"]
        available[0]["skills"] = []
        assert svc.identity.ensure()["config"]["skill_ids"] == []


def test_execution_rechecks_grants_without_opening_settings(sessions, available):
    from personal_agent.identity.capabilities import runtime_config
    from personal_agent.identity.capabilities import front_mcp_ids
    with sessions() as db:
        svc = AgentContext(db, "owner")
        saved = svc.identity.configure({"skill_ids": [], "mcp_ids": ["custom-tool", "web_fetch"], "plugin_ids": []})["config"]
        assert front_mcp_ids(saved) == ["web_fetch"]
        available[0]["mcps"] = ["web_fetch"]
        assert capability_ids(runtime_config(db, "owner", saved)) == ([], ["web_fetch"])
        available[0]["mcps"] = None
        assert capability_ids(runtime_config(db, "owner", saved)) == ([], [])

def test_settings_api_accepts_empty_lists(sessions, available):
    from personal_agent.api.schemas import Settings; from personal_agent.api.identity import configure; from personal_agent.api.identity import capability_options
    with sessions() as db:
        svc = AgentContext(db, "owner")
        configure(Settings(skill_ids=[], mcp_ids=[], plugin_ids=[]), svc=svc)
        assert capability_ids(svc.identity.ensure()["config"]) == ([], [])
        options = capability_options(svc=svc)
        assert "browser-automation" not in str({k: v for k, v in options["data"].items() if k != "plugin_component_ids"})


@pytest.mark.asyncio
async def test_explicit_profile_does_not_restore_history_or_skill_dependencies(monkeypatch):
    import logging
    from types import SimpleNamespace
    from core.llm.factory.selection.sticky_capabilities import sticky_capabilities
    from core.llm.factory.selection.progressive_bindings import progressive_bindings
    from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
    monkeypatch.setattr('core.harness.registry.agent_profile', lambda name: SimpleNamespace(inherit_default_mcps=False))
    def forbidden(**kwargs):
        pytest.fail("Explicit profile must not restore historical capability grants")
    monkeypatch.setattr('core.capabilities.plugins.runtime.resolve_sticky_plugin_capabilities', forbidden)
    request = SimpleNamespace(enabled_skill_ids=["document"], enabled_mcp_ids=["web_fetch"],
        enabled_kb_ids=[], derived_main=False, chat_id="old-task", current_user_id="owner",
        disable_tools=False, turbo_mode=False, user_agent=None, profile="personal_agent")
    log = logging.getLogger(__name__)
    await sticky_capabilities(request, _elapsed=lambda: "", _eval_scope=None, _log=log,
        _mode_manual_invoke=False, _note_unavailable=lambda _: None,
        required=RequiredCapabilities(), sticky=StickyCapabilities())
    monkeypatch.setattr('core.llm.factory.selection.capabilities.filter_skill_ids_for_user', lambda ids, uid: ids)
    monkeypatch.setattr("core.llm.factory.selection.capabilities._mcp_ids_bound_to_skills",
                        lambda ids: ["browser_runtime", "web_fetch", "other"])
    await progressive_bindings(request, _eval_scope=None, _log=log)
    assert request.enabled_mcp_ids == ["web_fetch"]


def test_browser_alias_components_cannot_be_selected(sessions, available):
    available[0]["mcps"].append("account-browser")
    available[1][0].update(install_id="plugin:account:browser-automation", mcp=["account-browser"])
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert "account-browser" not in svc.identity.capability_options()["mcp_ids"]
        with pytest.raises(ValueError):
            svc.identity.configure({"mcp_ids": ["account-browser"]})

def test_unavailable_saved_choices_survive_other_edits(sessions, available):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": [], "plugin_ids": []})
        available[0]["skills"] = []
        svc.identity.ensure()
        cfg = svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": ["web_fetch"], "plugin_ids": []})["config"]
        assert cfg["skill_ids"] == []
        assert cfg["capability_selection"]["skill_ids"] == ["custom-skill"]

def test_stale_session_cannot_restore_removed_binding(sessions, available):
    with sessions() as first, sessions() as second:
        old = AgentContext(first, "owner")
        old.identity.ensure()
        # Keep a strong reference: the identity map must contain the stale row.
        stale = old.identity.agent()
        current = AgentContext(second, "owner")
        current.identity.configure({"skill_ids": [], "mcp_ids": [], "plugin_ids": []})
        old.identity.configure({"timezone": "UTC"})
        first.refresh(stale)
        assert capability_ids(old.identity.view()["config"]) == ([], [])


def test_plugin_components_only_execute_through_selected_plugin(sessions, available):
    from personal_agent.identity.capabilities import runtime_config
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        options = svc.identity.capability_options()
        assert "plugin-skill" not in options["skill_ids"]
        assert "plugin-tool" not in options["mcp_ids"]
        for patch in ({"skill_ids": ["plugin-skill"]}, {"mcp_ids": ["plugin-tool"]}):
            with pytest.raises(ValueError):
                svc.identity.configure(patch)
        config = svc.identity.configure({"skill_ids": [], "mcp_ids": [], "plugin_ids": ["sample@owner"]})["config"]
        assert config["skill_ids"] == config["mcp_ids"] == []
        assert capability_ids(runtime_config(db, "owner", config)) == (["plugin-skill"], ["plugin-tool"])
        # Legacy direct bindings must not survive removal of their plugin.
        stale = {**config, "capability_selection": {"skill_ids": ["plugin-skill"], "mcp_ids": ["plugin-tool"], "plugin_ids": []}}
        assert capability_ids(runtime_config(db, "owner", stale)) == ([], [])
        row = svc.identity.agent()
        row.config = stale
        db.commit()
        restored = svc.identity.ensure()["config"]["capability_selection"]
        assert restored == {"skill_ids": [], "mcp_ids": [], "plugin_ids": []}
        available[1][1]["enabled"] = False
        assert "plugin-skill" not in svc.identity.capability_options()["skill_ids"]


def test_shadowed_private_plugin_components_are_not_standalone(sessions, available, monkeypatch):
    private = {"install_id": "sample@owner", "slug": "sample", "enabled": True,
               "skills": ["private-component"], "mcp": []}
    global_plugin = {"install_id": "sample@global", "slug": "sample", "enabled": True,
                     "skills": ["plugin-skill"], "mcp": ["plugin-tool"]}
    available[0]["skills"].append("private-component")
    monkeypatch.setattr('core.capabilities.plugins.management.list_installed',
                        lambda db, **kw: [global_plugin] if kw.get("include_global") else [private])
    with sessions() as db:
        svc = AgentContext(db, "owner")
        options = svc.identity.capability_options()
        assert "private-component" not in options["skill_ids"]
        assert [p["id"] for p in options["plugins"]] == ["sample@global"]
        with pytest.raises(ValueError):
            svc.identity.configure({"skill_ids": ["private-component"]})
