"""Read-only repository probe, synthetic temporary capability data only."""

import pytest
from tests.capabilities.test_runtime_recovery import durable_index, state, _zip
from core.capabilities import runtime, registry, skills
from core.capabilities.agents import AgentDefinition
from core.capabilities.errors import PackageMissing
from core.capabilities.ref import profile_id, cloud_ref
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services.desktop_capability_protocol import skill_content_hash


@pytest.fixture
def setup(durable_index, caps_root, monkeypatch):
    st = state("a")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    for name in ("cloud-a", "cloud-b"):
        body = "---\nname: " + name + "\ndescription: synthetic\n---\n" + name
        inst = registry.upsert(
            profile_id=profile_id(st["cloud_base"], "a"),
            ref=cloud_ref(st["cloud_base"], "skill", name, scope="shared"),
            content_hash=skill_content_hash(body, {}),
        )
        monkeypatch.setattr(
            cloud_skills, "_download", lambda *_, body=body: _zip({"SKILL.md": body})
        )
        cloud_skills.prepare_one(st, inst.install_id)
    return st


def test_scoped_steps_keep_independent_bindings_and_reports(setup):
    first_scope = runtime.child_scope("", "plan", "plan-1", "step-a")
    second_scope = runtime.child_scope("", "plan", "plan-1", "step-b")
    assert first_scope != second_scope and first_scope == runtime.child_scope(
        "", "plan", "plan-1", "step-a"
    )
    assert len(runtime.child_scope(first_scope, "subagent", "call-1", "agent-a")) <= 80
    first = runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id=first_scope)
    first = runtime.preflight(first, skill_ids=["cloud-a"], available_models=set())
    first_saved = first.to_dict()
    second = runtime.prepare("root-run", "owner", skill_ids=["cloud-b"], scope_id=second_scope)
    second = runtime.preflight(second, skill_ids=["cloud-b"], available_models=set())
    assert first.run_id == second.run_id == "root-run"
    assert first.scope_id != second.scope_id and first.view_dir != second.view_dir
    assert set(first.bindings) == {"cloud-a"} and set(second.bindings) == {"cloud-b"}
    assert runtime.get("root-run", scope_id=first_scope).to_dict() == first_saved
    assert runtime.get("root-run") is None
    assert runtime.view_for_execution("root-run", "owner", scope_id=second_scope) == second.view_dir
    assert runtime.frozen_loader(second).get_skill_dir("cloud-a") is None
    assert runtime.references("skill", second.profile, "cloud-b") == ["root-run"]


def test_same_scope_replay_retains_revision_and_rechecks_identity(setup, monkeypatch):
    scope = runtime.child_scope("", "subagent", "durable-call", "agent-a")
    first = runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id=scope)
    saved = first.to_dict()
    replay = runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id=scope)
    assert replay.to_dict() == saved
    with pytest.raises(PackageMissing):
        runtime.prepare("root-run", "owner", skill_ids=["cloud-b"], scope_id=scope)
    from core.capabilities.errors import PermissionDenied, CloudUnavailable

    with pytest.raises(PermissionDenied):
        runtime.prepare("root-run", "other", skill_ids=["cloud-a"], scope_id=scope)
    monkeypatch.setattr(bridge, "get_state", lambda: state("b"))
    with pytest.raises((PermissionDenied, CloudUnavailable)):
        runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id=scope)


def test_agent_snapshots_pin_per_scope_and_legacy_root_key_is_unchanged(setup):
    import hashlib
    from core.db.models import ContentBlock

    a = AgentDefinition(agent_id="same-agent", name="A", system_prompt="v1")
    b = AgentDefinition(agent_id="same-agent", name="A", system_prompt="v2")
    first_scope = runtime.child_scope("", "plan", "p", "step-a")
    second_scope = runtime.child_scope("", "plan", "p", "step-b")
    runtime.pin_agent_definition("root-run", "owner", a, scope_id=first_scope)
    runtime.pin_agent_definition("root-run", "owner", b, scope_id=second_scope)
    assert (
        runtime.pin_agent_definition("root-run", "owner", b, scope_id=first_scope).system_prompt
        == "v1"
    )
    runtime.pin_agent_definition("legacy", "owner", a)
    legacy = runtime.prepare("legacy", "owner", skill_ids=[])
    with registry._session() as db:
        assert db.get(
            ContentBlock, "desktop_capability_run:" + hashlib.sha256(b"legacy").hexdigest()
        )
        assert db.get(
            ContentBlock,
            "desktop_capability_agent:" + hashlib.sha256(b"legacy:same-agent").hexdigest(),
        )
    assert legacy.scope_id == ""


def test_tool_scope_recovery_allows_proven_root_and_rejects_children_and_unknown(setup):
    root = runtime.prepare("root-run", "owner", skill_ids=[])
    child = runtime.prepare(
        "root-run",
        "owner",
        skill_ids=["cloud-a"],
        scope_id=runtime.child_scope("", "subagent", "call-1", "agent"),
    )
    runtime.record_tool_scope(root, "root-tool", "view_text_file")
    runtime.record_tool_scope(root, "root-tool", "view_text_file")
    runtime.record_tool_scope(child, "child-tool", "view_text_file")
    runtime.require_root_tool_scope("root-run", "owner", "root-tool", "view_text_file")
    from core.capabilities.errors import IntegrityFailed

    with pytest.raises(IntegrityFailed):
        runtime.require_root_tool_scope("root-run", "owner", "child-tool", "view_text_file")
    with pytest.raises(IntegrityFailed):
        runtime.require_root_tool_scope("root-run", "owner", "unknown-old-tool", "view_text_file")
    with pytest.raises(IntegrityFailed):
        runtime.record_tool_scope(child, "root-tool", "view_text_file")
    with pytest.raises(IntegrityFailed):
        runtime.record_tool_scope(child, "", "view_text_file")


def test_same_scope_frozen_skill_bytes_survive_local_update(setup):
    body = "---\nname: local-a\ndescription: synthetic\n---\nv1"
    skills.publish_local_skill(
        "local-a",
        files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}),
        owner_user_id="owner",
    )
    scope = runtime.child_scope("", "plan", "p", "s")
    first = runtime.prepare("root-run", "owner", skill_ids=["local-a"], scope_id=scope)
    old_revision = first.bindings["local-a"]["revision"]
    newer = body.replace("v1", "v2")
    skills.publish_local_skill(
        "local-a",
        files={"SKILL.md": newer},
        content_hash=skill_content_hash(newer, {}),
        owner_user_id="owner",
    )
    replay = runtime.prepare("root-run", "owner", skill_ids=["local-a"], scope_id=scope)
    assert replay.bindings["local-a"]["revision"] == old_revision
    assert (replay.view_dir / "local-a" / "SKILL.md").read_text().endswith("v1")


@pytest.mark.asyncio
async def test_actual_plan_steps_use_stable_independent_scopes(setup, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from orchestration.subagents import plan_mode
    from core.llm.middlewares import CURRENT_RUN_BINDING

    steps = [
        SimpleNamespace(
            step_id="step-" + name,
            step_order=n,
            title=name,
            expected_tools=[],
            expected_skills=["cloud-" + name],
            expected_agents=[],
        )
        for n, name in enumerate(("a", "b"), 1)
    ]
    plan = SimpleNamespace(
        status="approved",
        steps=steps,
        extra_data={},
        task_input="synthetic",
        title="synthetic",
        total_steps=2,
    )

    class Service:
        def __init__(self, db):
            pass

        def get_plan(self, *_):
            return plan

        def update_plan(self, *a, **kw):
            plan.status = kw.get("status", plan.status)

        def update_step(self, *a, **kw):
            pass

    async def noop(*a, **kw):
        return None

    async def history(*a, **kw):
        return []

    async def reply(*a, **kw):
        return SimpleNamespace(content="synthetic done")

    captured = []

    async def create(**kw):
        assert CURRENT_RUN_BINDING.get() == ("root-run", "ledger-owner")
        prepared = runtime.prepare(
            kw["run_id"],
            kw["current_user_id"],
            skill_ids=kw["enabled_skill_ids"],
            scope_id=kw["capability_scope"],
        )
        runtime.preflight(prepared, skill_ids=kw["enabled_skill_ids"], available_models=set())
        captured.append((kw["run_id"], kw["capability_scope"], tuple(kw["enabled_skill_ids"])))
        return SimpleNamespace(state=SimpleNamespace(context=[]), reply=reply), []

    monkeypatch.setattr(plan_mode, "PlanService", Service)
    monkeypatch.setattr(plan_mode, "create_agent_executor", create)
    monkeypatch.setattr(plan_mode, "_prepare_history", history)
    monkeypatch.setattr(
        plan_mode, "_resolve_plugin_capabilities", lambda db, uid, sids, mids: (sids, mids, [])
    )
    monkeypatch.setattr(plan_mode, "_load_visible_agents", lambda *a: [])
    monkeypatch.setattr(plan_mode, "_build_file_context", lambda *a, **kw: "")
    monkeypatch.setattr(plan_mode, "_build_plan_header_lines", lambda *a: ([], []))
    monkeypatch.setattr(plan_mode, "_build_step_instruction", lambda step, *a, **kw: step.step_id)
    monkeypatch.setattr(plan_mode, "is_run_cancelled", lambda *a: False)
    monkeypatch.setattr(plan_mode.log_writer, "start_subagent_log", noop)
    monkeypatch.setattr(plan_mode.log_writer, "finish_subagent_log", noop)
    monkeypatch.setattr(plan_mode.log_writer, "subagent_scope", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(
        "core.services.project_scope.build_project_ctx_from_chat_id", lambda *a: None
    )
    monkeypatch.setattr(
        "core.services.ontology_service.build_user_ontology_runtime", lambda **kw: (False, {})
    )
    binding = CURRENT_RUN_BINDING.set(("root-run", "ledger-owner"))
    try:
        for _ in range(2):
            plan.status = "approved"
            events = [
                event
                async for event in plan_mode.astream_execute_plan(
                    plan_id="plan-1",
                    user_id="owner",
                    db=SimpleNamespace(refresh=lambda *_: None),
                    run_id="root-run",
                )
            ]
            assert not any(e["type"] == "plan_error" for e in events)
            assert events[-1]["completed_steps"] == 2
    finally:
        CURRENT_RUN_BINDING.reset(binding)
    assert captured[:2] == captured[2:]
    assert captured[0][1] != captured[1][1]


def test_subagent_scope_uses_durable_call_not_presentation_id(setup):
    from core.llm.subagent_tool import _child_capability_runtime
    from core.llm.middlewares import CURRENT_TOOL_CALL_ID
    from core.capabilities.errors import IntegrityFailed

    parent = {
        "run_id": "root-run",
        "journal_owner": "owner-token",
        "capability_scope": "parent-scope",
    }
    with pytest.raises(IntegrityFailed):
        _child_capability_runtime(parent, "agent-a")
    binding = CURRENT_TOOL_CALL_ID.set("persisted-call-a")
    try:
        first = _child_capability_runtime(parent, "agent-a")
        assert first == _child_capability_runtime(parent, "agent-a")
        assert (
            first["capability_scope"]
            != _child_capability_runtime(parent, "agent-b")["capability_scope"]
        )
    finally:
        CURRENT_TOOL_CALL_ID.reset(binding)
    assert first["run_id"] == parent["run_id"] and first["journal_owner"] == parent["journal_owner"]
    assert parent["capability_scope"] == "parent-scope"


@pytest.mark.asyncio
async def test_actual_factory_pins_scope_without_changing_durable_binding(setup, monkeypatch):
    from core.llm.agent_factory import create_agent_executor
    from core.llm.middlewares import CURRENT_RUN_BINDING

    captured = []

    class Captured(Exception):
        pass

    original = runtime.pin_agent_definition

    def pin(rid, uid, definition, **kw):
        captured.append((rid, uid, kw["scope_id"]))
        original(rid, uid, definition, **kw)
        raise Captured()

    monkeypatch.setattr(runtime, "pin_agent_definition", pin)
    binding = CURRENT_RUN_BINDING.set(("root-run", "ledger-owner"))
    try:
        with pytest.raises(Captured):
            await create_agent_executor(
                user_agent=AgentDefinition(agent_id="agent-a", name="A"),
                current_user_id="owner",
                capability_scope="child-scope",
            )
        assert CURRENT_RUN_BINDING.get() == ("root-run", "ledger-owner")
    finally:
        CURRENT_RUN_BINDING.reset(binding)
    assert captured == [("root-run", "owner", "child-scope")]


@pytest.mark.asyncio
async def test_recovery_adapter_executes_proven_root_but_never_rebuilds_child(setup, monkeypatch):
    from types import SimpleNamespace
    from agentscope.tool import ToolResponse
    from agentscope.message import TextBlock
    from orchestration import tool_effect_recovery
    from core.services.tool_effect_ledger import ToolEffectError

    root = runtime.prepare("root-run", "owner", skill_ids=[])
    child = runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id="child-scope")
    runtime.record_tool_scope(root, "root-tool", "view_text_file")
    runtime.record_tool_scope(child, "child-tool", "view_text_file")

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a):
            return SimpleNamespace(
                recovery_snapshot={"worker_args": {"context": {}}},
                user_id="owner",
                chat_id="synthetic-chat",
            )

    made = []

    async def call_tool(tc, st):
        yield ToolResponse(content=[TextBlock(type="text", text="synthetic recovered")])

    async def create(**kw):
        made.append(kw)
        return (
            SimpleNamespace(
                state=SimpleNamespace(apply_request_context=lambda *a: None),
                toolkit=SimpleNamespace(call_tool=call_tool),
            ),
            [],
        )

    monkeypatch.setattr(tool_effect_recovery, "SessionLocal", Db)
    monkeypatch.setattr("core.llm.agent_factory.create_agent_executor", create)
    intent = SimpleNamespace(
        run_id="root-run",
        tool_name="view_text_file",
        tool_call_id="root-tool",
        effect_id="effect-root",
        redacted_args={},
    )
    result = await tool_effect_recovery.replay_tool_intent(intent)
    assert result["tool_response"] and len(made) == 1
    intent.tool_call_id = "child-tool"
    with pytest.raises(ToolEffectError, match="original capability scope"):
        await tool_effect_recovery.replay_tool_intent(intent)
    intent.tool_call_id = "old-unproven-tool"
    with pytest.raises(ToolEffectError, match="original capability scope"):
        await tool_effect_recovery.replay_tool_intent(intent)
    assert len(made) == 1


@pytest.mark.asyncio
async def test_registered_child_dispatch_has_scope_even_without_sse(setup, monkeypatch):
    from types import SimpleNamespace
    from core.llm import subagent_tool
    from core.llm.middlewares import CURRENT_TOOL_CALL_ID

    registered = {}
    toolkit = SimpleNamespace(register_tool_function=lambda fn, **kw: registered.update(call=fn))
    captured = []

    def worker(*args):
        captured.append(args[8])
        return True, "synthetic child", [], []

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(subagent_tool, "_run_subagent_in_thread", worker)
    monkeypatch.setattr(subagent_tool.log_writer, "start_subagent_log", noop)
    monkeypatch.setattr(subagent_tool.log_writer, "finish_subagent_log", noop)
    monkeypatch.setattr(subagent_tool.subagent_sessions, "save", noop)
    parent = {
        "run_id": "root-run",
        "journal_owner": "ledger-owner",
        "capability_scope": "plan-scope",
    }
    subagent_tool.register_subagent_tool(
        toolkit, [{"agent_id": "agent-a", "name": "A"}], "owner", parent_runtime=parent
    )
    binding = CURRENT_TOOL_CALL_ID.set("persisted-tool-call")
    try:
        result = await registered["call"]("agent-a", "synthetic task")
    finally:
        CURRENT_TOOL_CALL_ID.reset(binding)
    assert len(captured) == 1
    assert captured[0]["capability_scope"] == runtime.child_scope(
        "plan-scope", "subagent", "persisted-tool-call", "agent-a"
    )
    assert captured[0]["run_id"] == "root-run" and captured[0]["journal_owner"] == "ledger-owner"
    rejected = await registered["call"]("agent-a", "synthetic task")
    assert rejected.state.value == "error" and len(captured) == 1


def test_same_ready_scope_never_replaces_its_frozen_dependency_report(setup):
    from core.capabilities.errors import IntegrityFailed

    for name in ("local-a", "local-b"):
        body = "---\nname: " + name + "\ndescription: synthetic\n---\n" + name
        skills.publish_local_skill(
            name,
            files={"SKILL.md": body},
            content_hash=skill_content_hash(body, {}),
            owner_user_id="owner",
        )
    scope = runtime.child_scope("", "plan", "p", "s")
    prepared = runtime.prepare("root-run", "owner", skill_ids=["local-a"], scope_id=scope)
    frozen = runtime.preflight(prepared, skill_ids=["local-a"], available_models=set())
    with pytest.raises(IntegrityFailed):
        runtime.preflight(frozen, skill_ids=["local-b"], available_models=set())
    blocked = runtime.get("root-run", scope_id=scope).dependency_report
    assert blocked["nodes"] == frozen.dependency_report["nodes"] and blocked["state"] == "blocked"
    subset = runtime.preflight(frozen, skill_ids=[], available_models=set())
    assert subset.dependency_report == frozen.dependency_report


def test_revocation_updates_readiness_without_destroying_frozen_nodes(setup):
    from core.capabilities.errors import PermissionDenied

    scope = runtime.child_scope("", "plan", "p", "s")
    prepared = runtime.prepare("root-run", "owner", skill_ids=["cloud-a"], scope_id=scope)
    frozen = runtime.preflight(prepared, skill_ids=["cloud-a"], available_models=set())
    inst = registry.get(frozen.bindings["cloud-a"]["install_id"])
    registry.set_enabled(inst.install_id, False)
    with pytest.raises(PermissionDenied):
        runtime.preflight(frozen, skill_ids=["cloud-a"], available_models=set())
    blocked = runtime.get("root-run", scope_id=scope).dependency_report
    assert blocked["state"] == "blocked" and blocked["ready"] is False
    assert blocked["frozen"] is True and blocked["errors"][0]["code"] == "permission_denied"
    assert blocked["nodes"] == frozen.dependency_report["nodes"]
