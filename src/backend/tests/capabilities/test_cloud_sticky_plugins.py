"""Persisted cloud plugin selections retain their scoped dependency closure."""

import json
import pytest
from core.capabilities import dependency, plugins, registry, runtime, skills, store
from core.capabilities.errors import PermissionDenied
from core.capabilities.paths import revision_for_hash
from core.db.models import ChatSession, InstalledPlugin, UserShadow
from core.llm import plugin_loader
from core.services.desktop_capability_protocol import entity_content_hash
from tests.capabilities.test_cloud_plugin_binding_runtime import cloud_plugin
from tests.capabilities.test_runtime_recovery import durable_index, state


@pytest.fixture
def selected(durable_index, caps_root, monkeypatch):
    st, plugin, comp, skill = cloud_plugin(monkeypatch, durable_index)
    with durable_index() as db:
        for model in (UserShadow, ChatSession, InstalledPlugin):
            model.__table__.create(db.get_bind(), checkfirst=True)
        db.add(UserShadow(user_id="local-owner", username="synthetic-owner"))
        db.add(ChatSession(chat_id="sticky-cloud", user_id="local-owner", title="synthetic"))
        db.commit()
    prepared = store.write_from_files(
        "skill",
        skill.profile_id,
        skill.key,
        revision_for_hash(skill.content_hash),
        {"SKILL.md": "private script"},
    )
    registry.set_state(skill.install_id, "ready", resolved_revision=prepared.revision)
    return st, plugin, comp, skill


def activate(plugin):
    plugin_loader.record_plugin_activation("sticky-cloud", [plugin.install_id])
    return plugin_loader.resolve_sticky_plugin_capabilities(
        user_id="local-owner", chat_id="sticky-cloud"
    )


def test_real_persisted_selection_restores_cloud_plugin_and_full_preflight(selected):
    _, plugin, _, skill = selected
    restored = activate(plugin)
    assert restored.install_ids == [plugin.install_id]
    assert restored.skill_ids == [skill.key]
    assert restored.mcp_ids == ["pack-search"]
    run = runtime.prepare(
        "sticky-turn", "local-owner", skill_ids=restored.skill_ids, plugin_ids=restored.install_ids
    )
    run = runtime.preflight(
        run,
        skill_ids=restored.skill_ids,
        plugin_ids=restored.install_ids,
        available_mcp=restored.mcp_ids,
    )
    assert any(node["install_id"] == plugin.install_id for node in run.dependency_report["nodes"])


@pytest.mark.parametrize("kind, field", [("agent", "agents"), ("plugin", "plugins")])
def test_sticky_complete_plugin_declarations_block_missing_required_component(
    selected, kind, field
):
    _, plugin, comp, _ = selected
    definition = json.loads(comp.entry_file.read_text())
    definition["components"][field] = [{"id": "missing-reviewer", "required": True}]
    files = plugins.plugin_manifest_files(definition)
    digest = entity_content_hash(files)
    newer = store.write_from_files(
        "plugin", plugin.profile_id, plugin.key, revision_for_hash(digest), files
    )
    registry.upsert(
        profile_id=plugin.profile_id, ref=plugin.ref, content_hash=digest, payload=plugin.payload
    )
    registry.set_state(plugin.install_id, "ready", resolved_revision=newer.revision)
    restored = activate(plugin)
    assert restored.install_ids == [plugin.install_id]
    run = runtime.prepare(
        "sticky-blocked",
        "local-owner",
        skill_ids=restored.skill_ids,
        plugin_ids=restored.install_ids,
    )
    with pytest.raises(dependency.DependencyMissing):
        runtime.preflight(
            run,
            skill_ids=restored.skill_ids,
            plugin_ids=restored.install_ids,
            available_mcp=restored.mcp_ids,
        )
    report = runtime.get(run.run_id).dependency_report
    assert any("missing-reviewer" in str(error["dependency_chain"]) for error in report["errors"])


@pytest.mark.parametrize("change", ["user", "account", "disable"])
def test_persisted_cloud_selection_rejects_identity_change_or_revocation(
    selected, monkeypatch, change
):
    from core.services import desktop_cloud_bridge as bridge

    _, plugin, _, _ = selected
    plugin_loader.record_plugin_activation("sticky-cloud", [plugin.install_id])
    user = "local-owner"
    if change == "user":
        user = "other-local-user"
    elif change == "account":
        monkeypatch.setattr(bridge, "get_state", lambda: state("other-cloud-owner"))
    else:
        registry.set_enabled(plugin.install_id, False)
    with pytest.raises(PermissionDenied):
        plugin_loader.resolve_sticky_plugin_capabilities(user_id=user, chat_id="sticky-cloud")


def test_raw_explicit_cloud_id_is_saved_with_its_account_scope(selected):
    _, plugin, _, _ = selected
    plugin_loader.record_plugin_activation(
        "sticky-cloud", ["pack@cloud-owner"], user_id="local-owner"
    )
    assert plugin_loader.load_activated_plugin_slugs("sticky-cloud") == [plugin.install_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["sticky", "mode", "unselected-mode"])
async def test_actual_factory_passes_selected_full_plugin_to_prepare_and_preflight(
    selected, durable_index, monkeypatch, selection
):
    from types import SimpleNamespace
    from core.db.engine import Base
    from core.llm import agent_factory
    from core.services import desktop_cloud_bridge as bridge
    from core.services.mcp_service import McpServerConfigService

    _, plugin, _, skill = selected
    with durable_index() as db:
        Base.metadata.create_all(db.get_bind())
    if selection == "sticky":
        plugin_loader.record_plugin_activation("sticky-cloud", [plugin.install_id])
    mode = SimpleNamespace(
        plugin_ids=[plugin.install_id],
        manual_invoke_enabled=True,
        code_exec_enabled=False,
        skill_ids=[],
        mcp_server_ids=[],
    )
    monkeypatch.setattr("core.llm.tool_permissions.resolve_approval_mode", lambda *a, **kw: "auto")
    monkeypatch.setattr(agent_factory, "get_enabled_ids", lambda *a: [])
    monkeypatch.setattr(agent_factory, "_effective_main_available_skills", lambda: [])
    monkeypatch.setattr(agent_factory, "_filter_skill_ids_for_user", lambda ids, uid: ids)
    monkeypatch.setattr(agent_factory, "_mcp_ids_bound_to_skills", lambda *a: [])
    monkeypatch.setattr(
        agent_factory, "get_skill_loader", lambda: SimpleNamespace(get_skill_dir=lambda sid: None)
    )
    monkeypatch.setattr(
        McpServerConfigService,
        "get_instance",
        classmethod(
            lambda cls: SimpleNamespace(
                get_all_servers=lambda **kw: {}, get_owned_servers=lambda *a, **kw: {}
            )
        ),
    )
    monkeypatch.setattr(bridge, "cloud_gateway_mcp_configs", lambda *a, **kw: {})
    monkeypatch.setattr(
        agent_factory, "_effective_mcp_server_keys", lambda *a, **kw: ["pack-search"]
    )
    monkeypatch.setattr(
        agent_factory,
        "_filter_mcp_servers_by_keys",
        lambda *a, **kw: {
            "pack-search": {"transport": "stdio", "command": "synthetic-never-executed"}
        },
    )
    captured = {}
    real_prepare, real_preflight = runtime.prepare, runtime.preflight

    def prepare(*a, **kw):
        captured["prepare"] = list(kw["plugin_ids"])
        return real_prepare(*a, **kw)

    class PreflightReached(Exception):
        pass

    def preflight(*a, **kw):
        captured["preflight"] = list(kw["plugin_ids"])
        captured["report"] = real_preflight(*a, **kw).dependency_report
        raise PreflightReached()

    monkeypatch.setattr(runtime, "prepare", prepare)
    monkeypatch.setattr(runtime, "preflight", preflight)
    with pytest.raises(PreflightReached):
        await agent_factory.create_agent_executor(
            current_user_id="local-owner",
            chat_id="sticky-cloud",
            run_id="sticky-factory",
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_kb_ids=[],
            turbo_mode=selection == "mode",
            mode_spec=mode,
        )
    expected = [] if selection == "unselected-mode" else [plugin.install_id]
    assert captured["prepare"] == captured["preflight"] == expected
    assert captured["report"]["ready"]
    assert any(
        node["install_id"] == plugin.install_id for node in captured["report"]["nodes"]
    ) == bool(expected)
    if not expected:
        assert runtime.get("sticky-factory").profile is None


def test_saved_plugin_replay_uses_frozen_definition_after_manifest_update(selected):
    _, plugin, comp, _ = selected
    restored = activate(plugin)
    kwargs = dict(skill_ids=restored.skill_ids, plugin_ids=restored.install_ids)
    run = runtime.prepare("sticky-replay", "local-owner", **kwargs)
    first = runtime.preflight(run, available_mcp=restored.mcp_ids, **kwargs)
    definition = json.loads(comp.entry_file.read_text())
    definition["components"]["agents"] = [{"id": "new-unavailable-agent", "required": True}]
    files = plugins.plugin_manifest_files(definition)
    digest = entity_content_hash(files)
    newer = store.write_from_files(
        "plugin", plugin.profile_id, plugin.key, revision_for_hash(digest), files
    )
    registry.upsert(
        profile_id=plugin.profile_id, ref=plugin.ref, content_hash=digest, payload=plugin.payload
    )
    registry.set_state(plugin.install_id, "ready", resolved_revision=newer.revision)
    restored = activate(plugin)
    replay = runtime.prepare(
        "sticky-replay",
        "local-owner",
        skill_ids=restored.skill_ids,
        plugin_ids=restored.install_ids,
    )
    replay = runtime.preflight(
        replay,
        available_mcp=restored.mcp_ids,
        skill_ids=restored.skill_ids,
        plugin_ids=restored.install_ids,
    )
    assert replay.dependency_report == first.dependency_report
    assert any(
        node["revision"] == comp.revision
        for node in replay.dependency_report["nodes"]
        if node["install_id"] == plugin.install_id
    )
    fresh = runtime.prepare("sticky-next-turn", "local-owner", **kwargs)
    with pytest.raises(dependency.DependencyMissing):
        runtime.preflight(fresh, available_mcp=restored.mcp_ids, **kwargs)
