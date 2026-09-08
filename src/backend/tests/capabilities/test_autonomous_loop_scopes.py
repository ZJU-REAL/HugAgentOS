"""Autonomous loop phases retain durable root ownership and independent snapshots."""

from types import SimpleNamespace

import pytest

from core.capabilities import runtime, skills
from core.llm.middlewares import CURRENT_RUN_BINDING
from core.services.desktop_capability_protocol import skill_content_hash
from tests.capabilities.test_runtime_recovery import durable_index
from tests.orchestration.test_autonomous_loop_driver import Harness


@pytest.mark.asyncio
async def test_actual_loop_scout_then_two_workers_use_separate_factory_snapshots(
    durable_index, caps_root, monkeypatch
):
    from core.db.engine import Base
    from core.llm import agent_factory
    from core.services import desktop_cloud_bridge as bridge
    from core.services.mcp_service import McpServerConfigService
    from orchestration import autonomous_loop as loop, loop_planner
    from orchestration.loop_evaluator import GoalSpec

    with durable_index() as db:
        Base.metadata.create_all(db.get_bind())
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    for key in ["worker-a", "worker-b"]:
        text = f"---\nname: {key}\ndescription: synthetic\n---\n{key}\n"
        skills.publish_local_skill(
            key,
            files={"SKILL.md": text},
            content_hash=skill_content_hash(text, {}),
            owner_user_id="unit",
        )
    original_worker = loop._run_worker_iteration
    harness = Harness(
        monkeypatch, [{"id": "R1", "description": "first"}, {"id": "R2", "description": "second"}]
    )
    monkeypatch.setattr(loop, "scout_workspace", loop_planner.scout_workspace)

    async def nonempty(*args):
        return False

    monkeypatch.setattr(loop_planner, "_workspace_is_empty", nonempty)
    monkeypatch.setattr("core.llm.tool_permissions.resolve_approval_mode", lambda *a, **kw: "auto")
    monkeypatch.setattr(agent_factory, "get_enabled_ids", lambda *a: [])
    selected = []
    monkeypatch.setattr(agent_factory, "_effective_main_available_skills", lambda: list(selected))
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
    monkeypatch.setattr(agent_factory, "_effective_mcp_server_keys", lambda *a, **kw: [])
    monkeypatch.setattr(agent_factory, "_filter_mcp_servers_by_keys", lambda *a, **kw: {})
    original_preflight = runtime.preflight
    captured = []

    class Prepared(Exception):
        pass

    def preflight(run, **kwargs):
        assert CURRENT_RUN_BINDING.get() == ("loop-root", "journal-owner")
        kwargs["available_models"] = set()
        actual = original_preflight(run, **kwargs)
        captured.append(actual)
        raise Prepared()

    monkeypatch.setattr(runtime, "preflight", preflight)
    workers = []

    async def worker(**kwargs):
        selected[:] = ["worker-a" if not workers else "worker-b"]
        workers.append(kwargs)
        try:
            await original_worker(**kwargs)
        except Prepared:
            return {"text": "completed", "tokens": 1, "tool_calls": 1}
        raise AssertionError("must reach the real factory preflight")

    monkeypatch.setattr(loop, "_run_worker_iteration", worker)
    token = CURRENT_RUN_BINDING.set(("loop-root", "journal-owner"))
    try:
        result = await loop.run_autonomous_loop(
            loop_id="persistent-loop",
            user_id="unit",
            goal_spec=GoalSpec(objective="synthetic", acceptance_criteria=["done"]),
            budget=loop.LoopBudget(),
            session_id="shared-workspace",
        )
    finally:
        CURRENT_RUN_BINDING.reset(token)
    assert result.status == "completed", result.reason
    assert len(captured) == 3
    assert len({run.scope_id for run in captured}) == 3
    assert all(run.scope_id and run.run_id == "loop-root" for run in captured)
    assert captured[0].dependency_report["nodes"] == []
    assert {node["install_id"] for node in captured[1].dependency_report["nodes"]} == {
        "skill:local:worker-a"
    }
    assert {node["install_id"] for node in captured[2].dependency_report["nodes"]} == {
        "skill:local:worker-b"
    }
    assert len({run.view_dir for run in captured}) == 3
    for run in captured:
        assert runtime.get("loop-root", scope_id=run.scope_id).to_dict() == run.to_dict()
    assert runtime.get("loop-root") is None
    assert all(call["session_id"] == "shared-workspace" for call in workers)
    assert workers[0]["capability_scope"] == loop._loop_scope("persistent-loop", "worker", 1, "R1")
    assert workers[1]["capability_scope"] == loop._loop_scope("persistent-loop", "worker", 2, "R2")


@pytest.mark.asyncio
async def test_resumed_loop_uses_persisted_iteration_and_distinct_review_scopes(monkeypatch):
    from copy import deepcopy
    from orchestration import autonomous_loop as loop
    from orchestration.loop_evaluator import DONE, GoalSpec

    Harness(monkeypatch, [{"id": "R1", "description": "pending"}])
    ledger = loop._new_ledger("same goal", [{"id": "R1", "description": "pending"}])
    ledger.update(iteration=7, loop_id="durable-loop", criteria=["done"])
    calls = []

    async def worker(**kw):
        calls.append(("worker", kw))
        return {"text": "done", "tokens": 1, "tool_calls": 1}

    async def review(**kw):
        calls.append(("confirm" if kw.get("second_pass") else "review", kw))
        return {"verdict": DONE, "evidence": "synthetic", "feedback": ""}

    monkeypatch.setattr(loop, "_run_worker_iteration", worker)
    monkeypatch.setattr(loop, "review_requirement", review)
    token = CURRENT_RUN_BINDING.set(("durable-root", "owner"))
    try:
        for _ in range(2):
            result = await loop.run_autonomous_loop(
                loop_id="durable-loop",
                user_id="unit",
                goal_spec=GoalSpec(objective="same goal"),
                budget=loop.LoopBudget(),
                session_id="same-workspace",
                load_ledger=lambda: deepcopy(ledger),
            )
            assert result.status == "completed"
    finally:
        CURRENT_RUN_BINDING.reset(token)
    assert [phase for phase, _ in calls] == ["worker", "review", "confirm"] * 2
    first_scopes = [kw["capability_scope"] for _, kw in calls[:3]]
    assert len(set(first_scopes)) == 3
    assert first_scopes == [kw["capability_scope"] for _, kw in calls[3:]]
    for phase, kw in calls:
        assert kw["capability_scope"] == loop._loop_scope("durable-loop", phase, 8, "R1")
        assert kw["session_id"] == "same-workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["planner", "judge", "ontology"])
async def test_text_helpers_partition_factory_audit_scope(monkeypatch, helper):
    from core.llm import agent_factory
    from orchestration import loop_planner, loop_evaluator
    from orchestration.subagents import ontology_reviewer

    captured = []

    class Captured(Exception):
        pass

    async def factory(**kw):
        captured.append(kw)
        assert CURRENT_RUN_BINDING.get() == ("root", "owner")
        raise Captured()

    monkeypatch.setattr(agent_factory, "create_agent_executor", factory)
    token = CURRENT_RUN_BINDING.set(("root", "owner"))
    try:
        for parent_scope in ["iteration-1", "iteration-2", "iteration-1"]:
            with pytest.raises(Captured):
                if helper == "planner":
                    await loop_planner._plan_llm_once(
                        "synthetic", model_name=None, user_id="unit", capability_scope=parent_scope
                    )
                elif helper == "judge":
                    await loop_evaluator._judge_once(
                        "synthetic", model_name=None, user_id="unit", capability_scope=parent_scope
                    )
                else:
                    await ontology_reviewer._run_text_agent(
                        "synthetic",
                        model_name=None,
                        model_provider_id=None,
                        user_id="unit",
                        runtime={},
                        capability_scope=parent_scope,
                    )
    finally:
        CURRENT_RUN_BINDING.reset(token)
    assert all(kw["disable_tools"] and kw["capability_scope"] for kw in captured)
    assert captured[0]["capability_scope"] == captured[2]["capability_scope"]
    assert captured[0]["capability_scope"] != captured[1]["capability_scope"]
    assert all(kw["capability_scope"] not in ["iteration-1", "iteration-2"] for kw in captured)


@pytest.mark.asyncio
async def test_loop_reviewer_forwards_exact_durable_scope_to_factory(monkeypatch):
    from core.llm import agent_factory
    from core.services import log_service
    from orchestration.subagents import loop_reviewer

    captured = []

    async def factory(**kw):
        captured.append(kw)
        raise RuntimeError("synthetic stop before model")

    async def noop(*a, **kw):
        return "log"

    monkeypatch.setattr(agent_factory, "create_agent_executor", factory)
    monkeypatch.setattr(log_service, "start_subagent_log", noop)
    monkeypatch.setattr(log_service, "finish_subagent_log", noop)
    await loop_reviewer.review_requirement(
        objective="goal",
        requirement_desc="requirement",
        acceptance_criteria=["done"],
        worker_summary="work",
        session_id="same-workspace",
        user_id="unit",
        capability_scope="durable-review-scope",
    )
    assert len(captured) == 1
    assert captured[0]["capability_scope"] == "durable-review-scope"
    assert captured[0]["sandbox_session_id"] == "same-workspace"


@pytest.mark.asyncio
async def test_ontology_committee_keeps_parent_iteration_and_seat_scopes(monkeypatch):
    from orchestration.subagents import ontology_reviewer
    from tests.ontology.test_ontology_reviewer import _runtime, _disable_audit

    captured = []

    async def text_agent(*a, **kw):
        captured.append(kw["capability_scope"])
        return '{"verdict":"pass","evidence":["synthetic"],"feedback":""}'

    monkeypatch.setattr(ontology_reviewer, "_run_text_agent", text_agent)
    _disable_audit(monkeypatch)
    for parent_scope in ["worker-1", "worker-2", "worker-1"]:
        result = await ontology_reviewer.review_ontology_output(
            task="synthetic",
            answer="synthetic",
            runtime=_runtime(),
            trace=[],
            citations=[],
            user_id="unit",
            chat_id=None,
            model_name=None,
            capability_scope=parent_scope,
        )
        assert result["verdict"] == "pass"
    assert len(captured) == 9
    assert len(set(captured[:6])) == 6
    assert captured[:3] == captured[6:]
