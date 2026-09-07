"""Declared dependency closure fails closed without installing anything."""

import json
import pytest
from core.capabilities import dependency, plugins, agents, registry, skills
from core.services.desktop_capability_protocol import skill_content_hash
from tests.capabilities.test_runtime_recovery import durable_index


def plugin(name, components, **extra):
    plugins.publish_local_plugin(
        {"slug": name, "name": name, "components": components, **extra}, owner_user_id=None
    )
    return registry.get("plugin:local:" + name)


def test_required_optional_unknown_and_cycle(index_db, caps_root):
    first = plugin("first", {"plugins": ["second"]})
    plugin("second", {"plugins": ["first"]})
    report = dependency.check_installation(first)
    assert not report["ready"] and report["errors"][0]["reason"] == "dependency_cycle"
    assert report["errors"][0]["dependency_chain"] == [
        "plugin:local:first",
        "plugin:local:second",
        "plugin:local:first",
    ]
    optional = plugin("optional", {"skills": [{"id": "missing", "required": False}]})
    report = dependency.check_installation(optional)
    assert report["ready"] and len(report["warnings"]) == 1
    unknown = plugin("unknown", {"hooks": [{"id": "unimplemented", "required": True}]})
    report = dependency.check_installation(unknown)
    assert not report["ready"] and report["errors"][0]["reason"] == "dependency_kind_unsupported"


def test_version_platform_and_unknown_legacy_plugin(index_db, caps_root):
    skills.publish_local_skill(
        "dep", files={"SKILL.md": "dep"}, content_hash=skill_content_hash("dep", {}), version="1.0"
    )
    parent = plugin(
        "parent", {"skills": [{"id": "dep", "version_constraint": ">=2"}]}, platforms=["windows"]
    )
    report = dependency.check_installation(parent, platform_name="linux")
    assert {item["reason"] for item in report["errors"]} == {
        "platform_incompatible",
        "version_mismatch",
    }
    legacy = plugin("empty", {})
    assert (
        dependency.check_installation(legacy)["errors"][0]["reason"] == "legacy_components_unknown"
    )


def test_agent_binding_preflight_uses_authorized_sets(index_db, caps_root):
    agents.publish_local_agent(
        {
            "agent_id": "writer",
            "name": "Writer",
            "mcp_server_ids": ["search"],
            "kb_ids": ["private-kb"],
            "model_provider_id": "private-model",
        }
    )
    inst = registry.get("agent:local:writer")
    report = dependency.check_installation(
        inst, available_mcp={"search"}, available_kb=set(), available_models=set()
    )
    assert not report["ready"]
    assert {e["dependency_chain"][-1] for e in report["errors"]} == {
        "kb:private-kb",
        "model:private-model",
    }
    report = dependency.check_installation(
        inst,
        available_mcp={"search"},
        available_kb={"private-kb"},
        available_models={"private-model"},
    )
    assert report["ready"]
    assert report["nodes"][0]["platform_check"] == "not_declared"


def test_runtime_dependency_metadata_never_claims_unverified_packages(index_db, caps_root):
    body = (
        "---\nname: dep\ndescription: test\ndependencies:\n  npm: [unknown-npm-package]\n---\nbody"
    )
    skills.publish_local_skill(
        "dep", files={"SKILL.md": body}, content_hash=skill_content_hash(body, {})
    )
    inst = registry.get("skill:local:dep")
    report = dependency.check_installation(inst, runtime_versions={})
    assert report["errors"][0]["reason"] == "runtime_dependency_unverified"
    assert dependency.check_installation(inst, runtime_versions={"npm:unknown-npm-package": "1.0"})[
        "ready"
    ]


def test_private_component_cannot_become_ready_for_another_owner(index_db, caps_root):
    skills.publish_local_skill(
        "private",
        files={"SKILL.md": "private"},
        content_hash=skill_content_hash("private", {}),
        owner_user_id="owner",
    )
    parent = plugin("parent", {"skills": ["private"]})
    assert not dependency.check_installation(parent, user_id="other")["ready"]
    assert dependency.check_installation(parent, user_id="owner")["ready"]


def test_runtime_preflight_blocks_declared_mcp_and_agent_kb(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    body = "---\nname: needs-search\ndescription: Search\nmcp_servers: search\n---\nbody"
    skills.publish_local_skill(
        "needs-search", files={"SKILL.md": body}, content_hash=skill_content_hash(body, {})
    )
    run = runtime.prepare("needs-run", "u", skill_ids=["needs-search"])
    with pytest.raises(dependency.DependencyMissing):
        runtime.preflight(run, skill_ids=["needs-search"], available_mcp=[])
    definition = agents.AgentDefinition(agent_id="writer", name="Writer", kb_ids=["kb-private"])
    with pytest.raises(dependency.DependencyMissing):
        runtime.preflight(
            run,
            skill_ids=["needs-search"],
            agent_definition=definition,
            available_mcp=["search"],
            available_kb=[],
        )
    ready = runtime.preflight(
        run,
        skill_ids=["needs-search"],
        agent_definition=definition,
        available_mcp=["search"],
        available_kb=["kb-private"],
    )
    assert ready.dependency_report["ready"]


def test_plugin_closure_replay_keeps_original_definition_revision(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    for name in ("first", "second"):
        skills.publish_local_skill(
            name, files={"SKILL.md": name}, content_hash=skill_content_hash(name, {})
        )
    original = plugin("pack", {"skills": ["first"]}, version="1")
    run = runtime.prepare("plugin-run", "u", skill_ids=[])
    pinned = runtime.preflight(run, plugin_ids=["pack"])
    plugin("pack", {"skills": ["second"]}, version="2")
    replay = runtime.preflight(runtime.get("plugin-run"), plugin_ids=["pack"])
    assert {node["install_id"] for node in replay.dependency_report["nodes"]} == {
        "plugin:local:pack",
        "skill:local:first",
    }
    assert replay.dependency_report["nodes"][0]["revision"] == original.resolved_revision
    assert runtime.references("plugin", "local", "pack", original.resolved_revision) == [
        "plugin-run"
    ]
    plugins.remove_local_plugin("pack")
    assert registry.get("plugin:local:pack").state == "removed"
    with pytest.raises(Exception):
        runtime.validate(replay)


def test_agent_preserves_and_checks_declared_platform_extensions(index_db, caps_root):
    agents.publish_local_agent(
        {
            "agent_id": "declared",
            "name": "Declared",
            "platforms": ["windows"],
            "extensions": [{"id": "unsupported-hook", "required": True}],
        }
    )
    inst = registry.get("agent:local:declared")
    report = dependency.check_installation(inst, platform_name="linux")
    assert {error["reason"] for error in report["errors"]} == {
        "platform_incompatible",
        "dependency_kind_unsupported",
    }


def test_wrong_execution_plane_and_unverified_runtime_constraint_are_explicit(index_db, caps_root):
    parent = plugin(
        "plane",
        {"skills": [{"id": "missing", "required": False}]},
        execution_plane="cloud",
        python_version=">=4",
    )
    report = dependency.check_installation(parent)
    assert {error["reason"] for error in report["errors"]} == {
        "execution_plane_incompatible",
        "runtime_constraint_unverified",
    }
    assert report["warnings"]


def test_nested_plugin_readiness_uses_plugin_components(index_db, caps_root):
    plugin("child", {"skills": [{"id": "optional", "required": False}]})
    parent = plugin("parent", {"plugins": ["child"]})
    assert plugins.readiness(parent, cloud_server_ids=[])["ready"]


def test_extra_requirements_list_and_extension_constraints_survive_projection(index_db, caps_root):
    agents.publish_local_agent(
        {
            "agent_id": "contract",
            "name": "Contract",
            "extra_config": {"capability_requirements": [{"kind": "mcp", "id": "search"}]},
            "extensions": {"python_version": ">=4", "future_hook": {"required": False}},
        }
    )
    inst = registry.get("agent:local:contract")
    report = dependency.check_installation(inst, available_mcp=set())
    assert {error["reason"] for error in report["errors"]} == {
        "not_authorized_or_missing",
        "runtime_constraint_unverified",
    }
    assert [error["reason"] for error in report["warnings"]] == ["dependency_kind_unsupported"]


def test_legacy_plugin_catalog_projects_declarative_component_ids_without_mutation():
    from core.services.plugin_service import _component_keys

    declaration = {
        "skills": [
            "plain",
            {"id": "required", "version_constraint": ">=2"},
            {"key": "optional", "required": False},
        ],
        "mcp": [{"server_id": "search"}],
    }
    assert _component_keys(declaration, "skills") == ["plain", "required", "optional"]
    assert _component_keys(declaration, "mcp") == ["search"]
    assert declaration["skills"][1]["version_constraint"] == ">=2"
