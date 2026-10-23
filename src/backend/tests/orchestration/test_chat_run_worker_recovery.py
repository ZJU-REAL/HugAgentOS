"""Behavioral regression coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import core.runs.events.store as run_event_stream
import fakeredis.aioredis
import orchestration.loops.driver as driver
import orchestration.loops.run as loops_run
import orchestration.plans.execute_run as execute_run
import orchestration.plans.generate_run as generate_run
import orchestration.plans.mode as mode
import pytest
from core.db.models import ChatMessage, ChatRun, ChatRunOperation, ChatSession, Plan
from core.runs.journal import RunJournal
from core.runs.tool_effects.errors import ToolOutcomeUnknown
from tests.orchestration.recovery_test_support import recovery_env


@pytest.mark.asyncio
async def test_plan_worker_pauses_nested_unknown_tool_outcome(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-tool-unknown",
        message_id="msg-plan-tool-unknown",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_execute", "plan_id": "plan-1"},
        recovery_snapshot={"kind": "plan_execute", "worker_args": {"context": {}}},
    )
    import orchestration.plans.mode as plan_mode

    async def broken_plan(**_kwargs):
        raise ExceptionGroup("plan tool failed", [ToolOutcomeUnknown("effect-plan")])
        yield  # pragma: no cover

    monkeypatch.setattr(mode, "astream_execute_plan", broken_plan)
    await execute_run._run_plan_execute_workflow(
        run_id=row.run_id,
        plan_id="plan-1",
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        enabled_mcp_ids=[],
        enabled_skill_ids=[],
        enabled_kb_ids=[],
        enabled_agent_ids=[],
        session_messages=[],
        model_name="test-model",
    )

    with sessions() as db:
        paused = db.get(ChatRun, row.run_id)
        assert paused.status == "needs_attention"
        assert "effect-plan" in paused.failure_reason


@pytest.mark.asyncio
async def test_plan_generate_fences_late_message_after_lease_takeover(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-generate-fenced",
        message_id="msg-plan-generate-fenced",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_generate"},
        recovery_snapshot={"kind": "plan_generate", "worker_args": {}},
    )
    import orchestration.plans.mode as plan_mode

    async def generated_then_taken_over(**_kwargs):
        yield {
            "type": "plan_generated",
            "plan_id": "plan-generated",
            "title": "Durable plan",
            "description": "must be owner fenced",
            "steps": [{"title": "one"}],
            "usage": {"total_tokens": 3},
        }
        with sessions() as db:
            db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
                {
                    "lease_owner": "successor",
                    "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=300),
                }
            )
            db.commit()

    monkeypatch.setattr(mode, "astream_generate_plan", generated_then_taken_over)
    await generate_run._run_plan_generate_workflow(
        run_id=row.run_id,
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        task_description="make a plan",
        model_name="test-model",
        model_provider_id=None,
        enabled_mcp_ids=[],
        enabled_skill_ids=[],
        enabled_kb_ids=[],
        enabled_agent_ids=[],
        session_messages=[],
        uploaded_files=[],
        journal_owner="old-worker",
    )

    with sessions() as db:
        fenced = db.get(ChatRun, row.run_id)
        assert fenced.status == "running"
        assert fenced.lease_owner == "successor"
        assert db.get(ChatMessage, row.message_id) is None
        assert db.get(Plan, "plan-generated") is None
    emitted = await redis.xrange(run_event_stream.redis_stream_key(row.run_id), min="-", max="+")
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_plan_generate_commits_message_and_terminal_state_atomically(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-generate-complete",
        message_id="msg-plan-generate-complete",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_generate"},
        recovery_snapshot={"kind": "plan_generate", "worker_args": {}},
    )
    import orchestration.plans.mode as plan_mode

    async def generated(**_kwargs):
        yield {
            "type": "plan_generated",
            "plan_id": "plan-complete",
            "title": "Atomic plan",
            "description": "one transaction",
            "steps": [{"title": "one"}],
            "usage": {"total_tokens": 4},
        }

    monkeypatch.setattr(mode, "astream_generate_plan", generated)
    await generate_run._run_plan_generate_workflow(
        run_id=row.run_id,
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        task_description="make a plan",
        model_name="test-model",
        model_provider_id=None,
        enabled_mcp_ids=[],
        enabled_skill_ids=[],
        enabled_kb_ids=[],
        enabled_agent_ids=[],
        session_messages=[],
        uploaded_files=[],
        journal_owner="plan-worker",
    )

    with sessions() as db:
        completed = db.get(ChatRun, row.run_id)
        message = db.get(ChatMessage, row.message_id)
        assert completed.status == "completed"
        assert completed.run_phase == "completed"
        assert message.extra_data["plan_id"] == "plan-complete"
        plan = db.get(Plan, "plan-complete")
        assert plan is not None
        assert plan.user_id == row.user_id
        assert [step.title for step in plan.steps] == ["one"]
        operations = (
            db.query(ChatRunOperation.operation_type)
            .filter(ChatRunOperation.run_id == row.run_id)
            .order_by(ChatRunOperation.operation_seq)
            .all()
        )
        assert ("message_committed",) in operations


@pytest.mark.asyncio
async def test_autonomous_worker_pauses_nested_unknown_without_partial_stale_write(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-loop-tool-unknown",
        message_id="msg-loop-tool-unknown",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "autonomous_loop", "loop_id": "loop-1"},
        recovery_snapshot={"kind": "autonomous_loop", "worker_args": {"context": {}}},
    )
    import orchestration.loops.driver as autonomous_loop

    async def broken_loop(**_kwargs):
        raise ExceptionGroup("loop tool failed", [ToolOutcomeUnknown("effect-loop")])

    monkeypatch.setattr(driver, "run_autonomous_loop", broken_loop)
    await loops_run._run_autonomous_loop_workflow(
        run_id=row.run_id,
        loop_id="loop-1",
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        goal_spec={"objective": "finish safely", "acceptance_criteria": []},
        budget={},
        model_name="test-model",
    )

    with sessions() as db:
        paused = db.get(ChatRun, row.run_id)
        assert paused.status == "needs_attention"
        assert "effect-loop" in paused.failure_reason
        assert db.get(ChatMessage, row.message_id) is None


@pytest.mark.asyncio
async def test_autonomous_project_binding_is_rejected_after_lease_takeover(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-loop-project-fenced",
        message_id="msg-loop-project-fenced",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "autonomous_loop", "loop_id": "loop-project"},
        recovery_snapshot={"kind": "autonomous_loop", "worker_args": {}},
    )
    import orchestration.loops.driver as autonomous_loop
    from core.projects import edition as project_scope

    monkeypatch.setattr(
        project_scope,
        "build_project_ctx",
        lambda _db, project_id: {"project_id": project_id},
    )

    original_append = RunJournal.append_operation

    def append_after_takeover(self, run_id, *, operation_type, **kwargs):
        if operation_type == "loop_project_bound":
            with sessions() as db:
                db.query(ChatRun).filter(ChatRun.run_id == run_id).update(
                    {
                        "lease_owner": "successor",
                        "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=300),
                    }
                )
                db.commit()
        return original_append(
            self,
            run_id,
            operation_type=operation_type,
            **kwargs,
        )

    async def must_not_run(**_kwargs):
        pytest.fail("stale autonomous worker reached model execution")

    monkeypatch.setattr(RunJournal, "append_operation", append_after_takeover)
    monkeypatch.setattr(driver, "run_autonomous_loop", must_not_run)
    await loops_run._run_autonomous_loop_workflow(
        run_id=row.run_id,
        loop_id="loop-project",
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        goal_spec={"objective": "build", "acceptance_criteria": []},
        budget={},
        model_name="test-model",
        project_id="project-1",
        journal_owner="old-worker",
    )

    with sessions() as db:
        fenced = db.get(ChatRun, row.run_id)
        session = db.get(ChatSession, row.chat_id)
        assert fenced.status == "running"
        assert fenced.lease_owner == "successor"
        assert session.project_id is None


def test_retirement_removes_batch_run_ledger_without_losing_plan_recovery(recovery_env):
    from core.db.batch_retirement import retire_batch_execution
    from core.db.models import ToolEffectLease, ToolEffectLedger, ToolEffectReceipt

    sessions = recovery_env
    journal = RunJournal(sessions)
    old = journal.accept(
        run_id="run-retired-batch",
        message_id="msg-retired-batch",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "batch_item", "plan_id": "batch-1", "item_index": 2},
        recovery_snapshot={"kind": "batch_item", "worker_args": {"context": {}}},
    )
    plan = journal.accept(
        run_id="run-preserved-plan",
        message_id="msg-preserved-plan",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_execute", "plan_id": "plan-1"},
        recovery_snapshot={
            "kind": "plan_execute",
            "worker_args": {"context": {"keep": True}},
        },
    )
    assert journal.claim(old.run_id, owner="batch-old", lease_seconds=60)
    assert journal.needs_attention(
        old.run_id, owner="batch-old", reason="legacy effect unknown"
    )
    assert journal.claim(plan.run_id, owner="plan-live", lease_seconds=300)
    with sessions() as db:
        # RunJournal does not create history; seed the existing shared message.
        db.add(
            ChatMessage(
                message_id=old.message_id,
                chat_id="chat-1",
                chat_seq=1,
                role="assistant",
                content="Preserve the existing historical answer",
            )
        )
        for run, effect_id, tool, owner in (
            (old, "retired-effect", "legacy_tool", "batch-old"),
            (plan, "preserved-effect", "web_fetch", "plan-live"),
        ):
            db.add(
                ToolEffectLedger(
                    effect_id=effect_id,
                    result_id="result-" + effect_id,
                    run_id=run.run_id,
                    operation_seq=1,
                    event_type="intent",
                    tool_name=tool,
                    args_hash="a" * 64,
                    redacted_args={},
                    recovery_policy="never_replay",
                )
            )
            db.add(
                ToolEffectLease(effect_id=effect_id, run_id=run.run_id, run_owner=owner)
            )
            db.add(
                ToolEffectReceipt(
                    effect_id=effect_id,
                    tool_name=tool,
                    user_id="user-1",
                    result_payload={"recorded": True},
                )
            )
        db.commit()
        engine = db.get_bind()
    with engine.begin() as bind:
        result = retire_batch_execution(bind)
        assert result["chat_runs"] == 1
        assert result["tool_effect_ledger"] == 1
        assert result["tool_effect_leases"] == 1
        assert result["tool_effect_receipts"] == 1
        assert not any(retire_batch_execution(bind).values())
    with sessions() as db:
        assert db.get(ChatRun, old.run_id) is None
        assert (
            db.query(ToolEffectLedger).filter_by(effect_id="retired-effect").count()
            == 0
        )
        assert db.get(ToolEffectLease, "retired-effect") is None
        assert db.get(ToolEffectReceipt, "retired-effect") is None
        current = db.get(ChatRun, plan.run_id)
        assert current.status == "running"
        assert current.lease_owner == "plan-live"
        assert current.request_payload == {"kind": "plan_execute", "plan_id": "plan-1"}
        assert current.recovery_snapshot["worker_args"]["context"] == {"keep": True}
        assert (
            db.query(ToolEffectLedger).filter_by(effect_id="preserved-effect").count()
            == 1
        )
        assert db.get(ToolEffectLease, "preserved-effect").run_owner == "plan-live"
        assert db.get(ToolEffectReceipt, "preserved-effect").result_payload == {
            "recorded": True
        }
        assert db.get(ChatSession, "chat-1") is not None
        assert db.get(ChatMessage, old.message_id) is not None
    # Surviving modern plan ownership and side-effect pause fences remain enforceable.
    assert not journal.needs_attention(
        plan.run_id, owner="batch-old", reason="wrong owner"
    )
    assert journal.needs_attention(
        plan.run_id, owner="plan-live", reason="preserved pause fence"
    )
    with sessions() as db:
        paused = db.get(ChatRun, plan.run_id)
        assert paused.status == "needs_attention"
        assert paused.failure_reason == "preserved pause fence"
