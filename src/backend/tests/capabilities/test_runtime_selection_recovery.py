
"""Regressions for desktop identity, explicit bindings and immutable runs."""


import pytest
from core.capabilities import registry, skills
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services import desktop_cloud_bundles as bundles


from tests.capabilities.runtime_recovery_support import state, _zip, _intent, durable_index


@pytest.mark.parametrize("method", ["get_by_id", "get_raw_by_id"])
def test_cloud_agent_details_cannot_cross_local_user_identity(
    index_db, caps_root, monkeypatch, method
):
    from types import SimpleNamespace
    from core.capabilities import agents
    from core.services.user_agent_base import UserAgentBaseService

    current = agents.AgentDefinition(agent_id="private-agent", name="Private", origin="cloud")
    monkeypatch.setattr(agents, "account_definition", lambda _: current)
    monkeypatch.setattr(agents, "account_definitions", lambda: [current])
    monkeypatch.setattr(skills, "account_authorized_for", lambda uid: uid == "current-user")
    service = object.__new__(UserAgentBaseService)
    service.repo = SimpleNamespace(get_by_id=lambda _: None)
    with pytest.raises(LookupError):
        getattr(service, method)("private-agent", "old-user")
    assert getattr(service, method)("private-agent", "current-user") is not None
    assert not agents.resolve_visible("old-user", []).chosen
    assert (
        agents.resolve_visible("current-user", []).chosen["Private"].install_id
        == "agent:local:private-agent"
    )


def test_unselected_cloud_skill_does_not_block_local_run_offline(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import CloudUnavailable, PermissionDenied
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    skills.publish_local_skill(
        "local-copy",
        files={"SKILL.md": "offline"},
        content_hash=skill_content_hash("offline", {}),
        owner_user_id="owner",
    )
    monkeypatch.setattr(
        bridge,
        "ensure_current_authorization",
        lambda *_: (_ for _ in ()).throw(CloudUnavailable("offline")),
    )
    run = runtime.prepare("offline-local", "owner", skill_ids=["local-copy"])
    assert run.profile is None and set(run.bindings) == {"local-copy"}
    assert runtime.frozen_loader(run).get_skill_dir("example") is None
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    runtime.validate(run, user_id="owner")
    with pytest.raises(PermissionDenied):
        runtime.validate(run, user_id="someone-else")


def test_cloud_skill_declared_by_agent_is_included_in_frozen_selection(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime, agents

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    definition = agents.AgentDefinition(
        agent_id="declare", name="Declare", dependencies=[{"kind": "skill", "id": "example"}]
    )
    run = runtime.prepare("agent-dependency", "owner", skill_ids=[], agent_definition=definition)
    assert run.profile == inst.profile_id and "example" in run.bindings


def test_cloud_skill_declared_by_selected_plugin_is_frozen(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime, plugins

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    plugins.publish_local_plugin(
        {"slug": "pack", "install_id": "pack@owner", "components": {"skills": ["example"]}},
        owner_user_id="owner",
    )
    run = runtime.prepare("plugin-dependency", "owner", skill_ids=[], plugin_ids=["pack@owner"])
    assert "example" in run.bindings and run.profile == inst.profile_id
    ready = runtime.preflight(run, plugin_ids=["pack@owner"])
    assert ready.dependency_report["ready"]


def test_local_agent_replay_does_not_depend_on_cloud_session(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime, agents

    current = [state("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    definition = agents.AgentDefinition(agent_id="local", name="Local", system_prompt="offline")
    runtime.pin_agent_definition("local-agent", "owner", definition)
    current[0] = None
    updated = agents.AgentDefinition(agent_id="local", name="Local", system_prompt="new")
    assert runtime.pin_agent_definition("local-agent", "owner", updated).system_prompt == "offline"


def test_prepared_run_is_materialized_once_before_loading(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    body = "---\nname: example\ndescription: sample\n---\nv1"
    skills.publish_local_skill(
        "example", files={"SKILL.md": body}, content_hash=skill_content_hash(body, {})
    )
    builds = []
    from core.capabilities import view

    build = view.build_view

    def counted_build(*args, **kwargs):
        builds.append(args[0])
        return build(*args, **kwargs)

    monkeypatch.setattr(view, "build_view", counted_build)
    run = runtime.prepare("single-materialization", "u", skill_ids=["example"])
    loader = runtime.frozen_loader(run)
    assert loader.get_skill_dir("example") is not None
    assert len(builds) == 1
    skills.bump_view_generation()
    assert runtime.frozen_loader(run).get_skill_dir("example") is not None
    assert len(builds) == 2


@pytest.mark.parametrize("change", ["none", "bytes", "disabled", "removed"])
def test_changed_grant_or_package_drops_only_that_skill(
    durable_index, caps_root, monkeypatch, change
):
    """授权撤销或字节改动之后，这一份立刻从视图里摘掉，读取方再也拿不到它。"""
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    comp = skills.publish_local_skill(
        "gate-test",
        files={"SKILL.md": "initial"},
        content_hash=skill_content_hash("initial", {}),
        owner_user_id="owner",
    )
    run = runtime.prepare("gate-" + change, "owner", skill_ids=["gate-test"])
    assert runtime.frozen_loader(run).get_skill_dir("gate-test")
    runtime.bind_mcp(run, {}, None)
    if change == "bytes":
        (comp.path / "SKILL.md").write_text("changed")
    elif change == "disabled":
        registry.set_enabled("skill:local:gate-test", False)
    elif change == "removed":
        registry.mark_removed("skill:local:gate-test")
    finished = runtime.preflight(run, available_models=set())
    if change == "none":
        assert "gate-test" not in finished.unavailable
        assert runtime.frozen_loader(finished).get_skill_dir("gate-test")
        return
    assert "gate-test" in runtime.get(run.run_id, scope_id=run.scope_id).unavailable
    assert not (run.view_dir / "gate-test").exists()


def test_identical_local_publication_does_not_invalidate_resolution(index_db, caps_root):
    from core.services.desktop_capability_protocol import skill_content_hash

    kwargs = dict(
        files={"SKILL.md": "same"},
        content_hash=skill_content_hash("same", {}),
        owner_user_id="owner",
    )
    first = skills.publish_local_skill("stable", **kwargs)
    before = registry.generation(), skills.view_generation()
    second = skills.publish_local_skill("stable", **kwargs)
    assert first == second
    assert (registry.generation(), skills.view_generation()) == before
    skills.publish_local_skill("stable", **{**kwargs, "owner_user_id": "changed"})
    assert registry.generation() > before[0]
    assert skills.view_generation() > before[1]


def test_missing_view_target_never_reaches_the_sandbox(durable_index, caps_root, monkeypatch):
    """冻结的版本目录不见了：视图里不能留下这个名字，也不能悄悄指向别处。"""
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    run = runtime.prepare("missing-view-target", "u", skill_ids=["example"])
    monkeypatch.setattr("core.capabilities.runtime.state._component", lambda binding: None)
    runtime.rebuild(run)
    assert "example" in runtime.get("missing-view-target").unavailable
    assert not (run.view_dir / "example").exists()
