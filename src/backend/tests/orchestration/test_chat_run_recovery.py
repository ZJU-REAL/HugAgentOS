"""Public ChatRun executor integration for durable acceptance and restart recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.exception import DeveloperOrientedException
from agentscope.message import ToolCallBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.tool import FunctionTool, Toolkit
from api.routes.v1 import chats as chat_routes
from core.auth.backend import UserContext
from core.db.engine import Base
from core.db.models import (
    ChatMessage,
    ChatRun,
    ChatRunOperation,
    ChatSession,
    ChatSteerQueueItem,
    Plan,
)
from core.llm.middlewares import AgentRuntimeState, ToolEffectMiddleware
from core.services import ChatService
from core.services.run_journal import RunJournal
from core.services.steer_queue import SteerQueue
from core.services.tool_effect_ledger import (
    ToolEffectJournal,
    ToolOutcomeUnknown,
    recover_incomplete_tool_effects,
)
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def recovery_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'executor-recovery.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


@pytest.mark.asyncio
async def test_start_run_commits_acceptance_and_snapshot_before_registering_worker(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    observed = {}

    def register(run_id, coro, *, name):
        with sessions() as db:
            row = db.get(ChatRun, run_id)
            observed.update(
                exists=row is not None,
                status=row.status,
                phase=row.run_phase,
                snapshot=dict(row.recovery_snapshot or {}),
                name=name,
            )
        coro.close()

    monkeypatch.setattr(executor, "_register_run_task", register)

    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello with context",
        raw_user_message="hello",
        context={"user_id": "user-1", "enabled_mcp_ids": []},
        request_payload={"kind": "chat", "message": "hello"},
        model_name="test-model",
    )

    assert observed["exists"] is True
    assert observed["status"] == "pending"
    assert observed["phase"] == "accepted"
    assert observed["snapshot"]["kind"] == "chat"
    args = observed["snapshot"]["worker_args"]
    assert args["session_messages"][-1]["content"] == "hello"
    assert args["effective_user_message"] == "hello with context"
    assert args["model_name"] == "test-model"
    assert run.run_id.startswith("run_")


def test_active_run_probe_hides_internal_agent_rows(recovery_env):
    sessions = recovery_env
    journal = RunJournal(sessions)
    internal = journal.accept(
        run_id="run-internal-hidden",
        message_id="msg-internal-hidden",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "internal_job_agent"},
        recovery_snapshot={"kind": "internal_job_agent"},
    )
    assert journal.claim(internal.run_id, owner="internal", lease_seconds=60)

    assert executor.get_active_run_for_chat("chat-1", "user-1") is None

    public = journal.accept(
        run_id="run-public-visible",
        message_id="msg-public-visible",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(public.run_id, owner="public", lease_seconds=60)
    assert executor.get_active_run_for_chat("chat-1", "user-1").run_id == public.run_id


@pytest.mark.asyncio
async def test_public_worker_keeps_ambiguous_agent_tool_call_recoverable(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-agent-timeout",
        message_id="msg-agent-timeout",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    async def exploding_probe(value: int):
        """Simulate an adapter that never produces a ToolResponse."""
        del value
        raise DeveloperOrientedException("adapter response was lost")

    class ToolModel:
        model = "ambiguous-tool"
        context_size = 32768

        async def __call__(self, messages, tools=None, **_kwargs):
            del messages, tools
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="ambiguous-provider-call",
                        name="exploding_probe",
                        input='{"value":1}',
                    )
                ],
                is_last=False,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
            )

        async def count_tokens(self, messages, tools=None):
            del messages, tools
            return 1

    async def workflow(*, context, **_kwargs):
        agent = Agent(
            name="worker-public-seam",
            system_prompt="Call the tool.",
            model=ToolModel(),
            toolkit=Toolkit(tools=[FunctionTool(exploding_probe)]),
            middlewares=[ToolEffectMiddleware(session_factory=sessions)],
            state=AgentRuntimeState(
                run_id=context["run_id"],
                journal_owner=context["journal_owner"],
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            ),
            react_config=ReActConfig(max_iters=1),
        )
        await agent.reply(UserMsg(name="user", content="run"))
        if False:
            yield {}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    await executor._run_workflow(
        run_id=row.run_id,
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        session_messages=[{"role": "user", "content": "run"}],
        effective_user_message="run",
        raw_user_message="run",
        context={"user_id": row.user_id},
        model_name="test-model",
    )

    with sessions() as db:
        paused = db.get(ChatRun, row.run_id)
        assert paused.status == "needs_attention"
        assert paused.run_phase == "needs_attention"
    decisions = await recover_incomplete_tool_effects(journal=ToolEffectJournal(sessions))
    assert [item.action for item in decisions] == ["needs_attention"]


@pytest.mark.asyncio
async def test_legacy_regenerate_stream_injects_a_durable_tool_binding(recovery_env, monkeypatch):
    sessions = recovery_env
    captured = {}
    monkeypatch.setattr(chat_routes, "SessionLocal", sessions)

    async def workflow(*, context, **_kwargs):
        captured.update(context)
        yield {"type": "heartbeat"}

    monkeypatch.setattr(chat_routes, "astream_chat_workflow", workflow)
    with sessions() as db:
        frames = [
            item
            async for item in chat_routes._stream_sse_response(
                chat_service=ChatService(db),
                chat_id="chat-1",
                model_name="test-model",
                session_messages=[{"role": "user", "content": "retry"}],
                user_message="retry",
                context={"user_id": "user-1", "enabled_mcps": ["web-search"]},
                error_label="regenerate_failed",
                db=db,
                user_id="user-1",
            )
        ]

    assert frames == [": heartbeat\n\n"]
    assert captured["run_id"]
    assert captured["journal_owner"]
    with sessions() as db:
        row = db.get(ChatRun, captured["run_id"])
        assert row.status == "completed"
        assert row.request_payload["kind"] == "legacy_chat_stream"
        assert row.recovery_snapshot["worker_args"]["context"]["mcp_ids"] == ["web-search"]


@pytest.mark.asyncio
async def test_plan_worker_pauses_nested_unknown_tool_outcome(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-tool-unknown",
        message_id="msg-plan-tool-unknown",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_execute", "plan_id": "plan-1"},
        recovery_snapshot={"kind": "plan_execute", "worker_args": {"context": {}}},
    )
    from orchestration.subagents import plan_mode

    async def broken_plan(**_kwargs):
        raise ExceptionGroup("plan tool failed", [ToolOutcomeUnknown("effect-plan")])
        yield  # pragma: no cover

    monkeypatch.setattr(plan_mode, "astream_execute_plan", broken_plan)
    await executor._run_plan_execute_workflow(
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
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-generate-fenced",
        message_id="msg-plan-generate-fenced",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_generate"},
        recovery_snapshot={"kind": "plan_generate", "worker_args": {}},
    )
    from orchestration.subagents import plan_mode

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

    monkeypatch.setattr(plan_mode, "astream_generate_plan", generated_then_taken_over)
    await executor._run_plan_generate_workflow(
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
        assert fenced.last_event_offset == 2
        assert db.get(ChatMessage, row.message_id) is None
        assert db.get(Plan, "plan-generated") is None


@pytest.mark.asyncio
async def test_plan_generate_commits_message_and_terminal_state_atomically(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-plan-generate-complete",
        message_id="msg-plan-generate-complete",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_generate"},
        recovery_snapshot={"kind": "plan_generate", "worker_args": {}},
    )
    from orchestration.subagents import plan_mode

    async def generated(**_kwargs):
        yield {
            "type": "plan_generated",
            "plan_id": "plan-complete",
            "title": "Atomic plan",
            "description": "one transaction",
            "steps": [{"title": "one"}],
            "usage": {"total_tokens": 4},
        }

    monkeypatch.setattr(plan_mode, "astream_generate_plan", generated)
    await executor._run_plan_generate_workflow(
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
        assert completed.last_event_offset == 3
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
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-loop-tool-unknown",
        message_id="msg-loop-tool-unknown",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "autonomous_loop", "loop_id": "loop-1"},
        recovery_snapshot={"kind": "autonomous_loop", "worker_args": {"context": {}}},
    )
    from orchestration import autonomous_loop

    async def broken_loop(**_kwargs):
        raise ExceptionGroup("loop tool failed", [ToolOutcomeUnknown("effect-loop")])

    monkeypatch.setattr(autonomous_loop, "run_autonomous_loop", broken_loop)
    await executor._run_autonomous_loop_workflow(
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
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-loop-project-fenced",
        message_id="msg-loop-project-fenced",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "autonomous_loop", "loop_id": "loop-project"},
        recovery_snapshot={"kind": "autonomous_loop", "worker_args": {}},
    )
    from core.services import project_scope
    from orchestration import autonomous_loop

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
    monkeypatch.setattr(autonomous_loop, "run_autonomous_loop", must_not_run)
    await executor._run_autonomous_loop_workflow(
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


def test_batch_item_needs_attention_blocks_item_regeneration(recovery_env, monkeypatch):
    sessions = recovery_env
    from orchestration import batch_orchestrator

    monkeypatch.setattr(batch_orchestrator, "SessionLocal", sessions)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-batch-blocked",
        message_id="msg-batch-blocked",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "batch_item", "plan_id": "batch-1", "item_index": 2},
        recovery_snapshot={"kind": "batch_item", "worker_args": {"context": {}}},
    )
    assert journal.claim(row.run_id, owner="batch-old", lease_seconds=60)
    assert journal.needs_attention(row.run_id, owner="batch-old", reason="tool recovered")

    assert batch_orchestrator._blocking_item_run("batch-1", 2) == row.run_id
    assert batch_orchestrator._blocking_item_run("batch-1", 1) is None

    pending = journal.accept(
        run_id="run-batch-pending",
        message_id="msg-batch-pending",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "batch_item", "plan_id": "batch-1", "item_index": 3},
        recovery_snapshot={"kind": "batch_item", "worker_args": {"context": {}}},
    )
    running = journal.accept(
        run_id="run-batch-running",
        message_id="msg-batch-running",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "batch_item", "plan_id": "batch-1", "item_index": 4},
        recovery_snapshot={"kind": "batch_item", "worker_args": {"context": {}}},
    )
    assert journal.claim(running.run_id, owner="batch-live", lease_seconds=300)

    assert batch_orchestrator._blocking_item_run("batch-1", 3) == pending.run_id
    assert batch_orchestrator._blocking_item_run("batch-1", 4) == running.run_id


@pytest.mark.asyncio
async def test_automation_prompt_injects_durable_binding_and_recovery_surface(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    from core.db import engine as db_engine
    from core.services import ontology_service, user_model_selection
    from orchestration import workflow
    from orchestration.schedulers.automation_scheduler import AutomationScheduler

    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(
        user_model_selection,
        "resolve_effective_chat_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        ontology_service,
        "build_user_ontology_runtime",
        lambda **_kwargs: (False, {}),
    )
    captured = {}

    async def bound_workflow(*, context, **_kwargs):
        captured.update(context)
        yield {"type": "content", "delta": "automation result"}
        yield {"type": "meta", "usage": {"total_tokens": 2}}

    monkeypatch.setattr(workflow, "astream_chat_workflow", bound_workflow)
    chat_id, text, _usage = await AutomationScheduler()._execute_prompt_task(
        user_id="user-1",
        task_name="nightly",
        prompt="collect evidence",
        task_id="scheduled-1",
        enabled_mcp_ids=["web-search"],
        enabled_skill_ids=["research"],
        enabled_kb_ids=[],
    )

    assert text == "automation result"
    assert captured["run_id"]
    assert captured["journal_owner"]
    assert captured["automation_run"] is True
    with sessions() as db:
        row = db.get(ChatRun, captured["run_id"])
        assert row.chat_id == chat_id
        assert row.status == "completed"
        context = row.recovery_snapshot["worker_args"]["context"]
        assert context["automation_run"] is True
        assert context["mcp_ids"] == ["web-search"]
        assert context["skill_ids"] == ["research"]


@pytest.mark.asyncio
async def test_startup_recovery_re_registers_accepted_chat_without_failing_it(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-accepted",
        message_id="msg-accepted",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat", "message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )
    registered = []

    def register(run_id, coro, *, name):
        registered.append((run_id, name))
        coro.close()

    monkeypatch.setattr(executor, "_register_run_task", register)

    assert await executor.recover_orphan_runs() == 1
    assert registered == [(row.run_id, f"chat_run_recovery:{row.run_id}")]
    with sessions() as db:
        recovered = db.get(ChatRun, row.run_id)
        assert recovered.status == "pending"
        assert recovered.error_message is None


@pytest.mark.asyncio
async def test_startup_recovery_commits_saved_model_output_without_second_model_call(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-model-done",
        message_id="msg-model-done",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat", "message": "hello"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(row.run_id, owner="dead-worker", lease_seconds=60)
    journal.save_snapshot(
        row.run_id,
        owner="dead-worker",
        phase="model_completed",
        safety="replayable",
        snapshot={
            "assistant_content": "durable answer",
            "message_id": row.message_id,
            "model_name": "test-model",
            "tool_calls": [{"id": "read-1", "name": "read", "output": "ok"}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "extra_data": {"route": "main", "message_id": row.message_id},
        },
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()

    registered = []
    monkeypatch.setattr(
        executor,
        "_register_run_task",
        lambda *args, **kwargs: registered.append((args, kwargs)),
    )

    assert await executor.recover_orphan_runs() == 1
    assert registered == []
    with sessions() as db:
        recovered = db.get(ChatRun, row.run_id)
        message = db.get(ChatMessage, row.message_id)
        assert recovered.status == "completed"
        assert recovered.run_phase == "completed"
        assert message.content == "durable answer"
        assert message.usage == {"input_tokens": 3, "output_tokens": 2}


@pytest.mark.asyncio
async def test_model_snapshot_recovery_also_commits_queued_handoff(recovery_env, monkeypatch):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-model-handoff",
        message_id="msg-model-handoff",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat", "message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"chat_id": "chat-1", "user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )
    assert journal.claim(row.run_id, owner="dead-worker", lease_seconds=60)
    queued = SteerQueue(sessions).accept(
        target_run_id=row.run_id,
        chat_id="chat-1",
        user_id="user-1",
        steer_id="follow-after-recovery",
        message="恢复后继续执行",
        delivery_mode="follow_up",
        replace_latest=False,
    )
    journal.save_snapshot(
        row.run_id,
        owner="dead-worker",
        phase="model_completed",
        safety="replayable",
        snapshot={
            "assistant_content": "durable answer",
            "message_id": row.message_id,
            "model_name": "test-model",
            "usage": {},
            "context": {"chat_id": "chat-1", "user_id": "user-1"},
        },
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()

    registered = []

    def register(run_id, coro, *, name):
        registered.append((run_id, name))
        coro.close()

    projected_events = []

    async def capture_event(_run_id, _offset, event):
        projected_events.append(event)

    async def no_event_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(executor, "_register_run_task", register)
    monkeypatch.setattr(executor, "_xadd_event", capture_event)
    monkeypatch.setattr(executor, "_expire_stream", no_event_write)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    assert await executor.recover_orphan_runs() == 1
    assert len(registered) == 1
    assert registered[0][1].startswith("chat_run_handoff_recovery:")
    next_run_id = registered[0][0]
    assert [event["type"] for event in projected_events] == [
        "queued_run_started",
        executor._TERMINAL_TYPE,
    ]
    assert projected_events[0]["run_id"] == next_run_id
    assert projected_events[0]["message"] == "恢复后继续执行"
    with sessions() as db:
        recovered = db.get(ChatRun, row.run_id)
        next_run = db.get(ChatRun, next_run_id)
        applied = db.get(ChatSteerQueueItem, queued.queue_id)
        queued_message = (
            db.query(ChatMessage)
            .filter(ChatMessage.extra_data["steer_queue_id"].as_string() == queued.queue_id)
            .one()
        )
        assert recovered.status == "completed"
        assert next_run.status == "pending"
        assert applied.status == "applied"
        assert applied.applied_run_id == next_run_id
        assert queued_message.content == "恢复后继续执行"


@pytest.mark.asyncio
async def test_unknown_tool_result_pauses_and_emits_compatible_terminal_projection(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-unknown-tool",
        message_id="msg-unknown-tool",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(row.run_id, owner="dead-worker", lease_seconds=60)
    journal.append_operation(
        row.run_id,
        owner="dead-worker",
        operation_type="tool_result_observed",
        phase="tool_result_unknown",
        safety="unknown_side_effect",
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()
    projected = []

    async def terminal(run_id, *, chat_id, error_text, cancelled=False):
        projected.append((run_id, chat_id, error_text, cancelled))

    monkeypatch.setattr(executor, "_write_terminal_to_stream", terminal)

    assert await executor.recover_orphan_runs() == 1
    with sessions() as db:
        recovered = db.get(ChatRun, row.run_id)
        assert recovered.status == "needs_attention"
        assert recovered.failure_reason == "unknown tool result requires recovery decision"
    assert projected == [
        (
            row.run_id,
            "chat-1",
            "任务在工具结果不确定的安全边界暂停，等待恢复决策",
            False,
        )
    ]


@pytest.mark.asyncio
async def test_real_worker_journals_model_and_message_safe_points(recovery_env, monkeypatch):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-worker",
        message_id="msg-worker",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat", "message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )
    emitted = []

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "durable "}
        yield {"type": "content", "delta": "answer"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": False,
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }

    async def emit(_run_id, offset, event):
        emitted.append((offset, dict(event)))

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_xadd_event", emit)
    monkeypatch.setattr(executor, "_expire_stream", lambda _run_id: _async_none())
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    await executor._run_workflow(
        run_id=row.run_id,
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        model_name="test-model",
        journal_owner="worker:test",
    )

    with sessions() as db:
        completed = db.get(ChatRun, row.run_id)
        message = db.get(ChatMessage, row.message_id)
        operations = (
            db.query(ChatRunOperation)
            .filter(ChatRunOperation.run_id == row.run_id)
            .order_by(ChatRunOperation.operation_seq)
            .all()
        )
        assert completed.status == "completed"
        assert completed.run_phase == "completed"
        assert completed.lease_owner is None
        assert message.content == "durable answer"
        assert [item.operation_type for item in operations] == [
            "worker_started",
            "model_dispatch",
            "snapshot_saved",
            "message_committed",
            "run_completed",
        ]
        assert [item.operation_seq for item in operations] == [1, 2, 3, 4, 5]
    assert emitted[-1][1]["type"] == executor._TERMINAL_TYPE


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_public_start_follow_and_history_complete_on_durable_offsets(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "public answer"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": False,
            "usage": {"input_tokens": 2, "output_tokens": 2},
        }

    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )

    async def collect():
        return [event async for event in executor.follow_run(run.run_id)]

    events = await asyncio.wait_for(collect(), timeout=5)
    offsets = [event["_offset"] for event in events]
    assert offsets == sorted(set(offsets))
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "meta"
    with sessions() as db:
        completed = db.get(ChatRun, run.run_id)
        message = db.get(ChatMessage, run.message_id)
        assert completed.status == "completed"
        assert completed.last_event_offset == offsets[-1] + 1
        assert message.content == "public answer"


@pytest.mark.asyncio
async def test_public_follow_after_recovery_resets_partial_stream_and_keeps_offsets(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-partial-stream",
        message_id="msg-partial-stream",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )
    assert journal.claim(row.run_id, owner="dead-worker", lease_seconds=60)
    journal.append_operation(
        row.run_id,
        owner="dead-worker",
        operation_type="worker_started",
        phase="pre_model",
        safety="replayable",
    )
    for text in ("old partial one", "old partial two"):
        offset = journal.allocate_event_offset(row.run_id, owner="dead-worker")
        await executor._xadd_event(
            row.run_id,
            offset,
            {"type": "content", "delta": text, "chat_id": "chat-1"},
        )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "recovered answer"}
        yield {"type": "meta", "route": "main", "usage": {}}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    assert await executor.recover_orphan_runs() == 1

    async def collect_from_crash_offset():
        return [event async for event in executor.follow_run(row.run_id, from_offset=2)]

    events = await asyncio.wait_for(collect_from_crash_offset(), timeout=5)
    assert events[0]["_offset"] == 3
    assert all(event["_offset"] > 2 for event in events)
    assert any(event.get("reason") == "run_recovered" for event in events)
    assert events[-1]["type"] == "meta"
    raw_entries = await redis.xrange(executor._stream_key(row.run_id), min="-", max="+")
    decoded = [json.loads(fields["data"]) for _entry_id, fields in raw_entries]
    assert all("old partial" not in str(event) for event in decoded)
    assert decoded[-1]["type"] == executor._TERMINAL_TYPE


@pytest.mark.asyncio
async def test_active_run_probe_replays_the_complete_existing_prefix(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-refresh-replay",
        message_id="msg-refresh-replay",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(row.run_id, owner="live-worker", lease_seconds=60)
    for text in ("already produced one", "already produced two"):
        offset = journal.allocate_event_offset(row.run_id, owner="live-worker")
        await executor._xadd_event(
            row.run_id,
            offset,
            {"type": "content", "delta": text, "chat_id": "chat-1"},
        )

    with sessions() as db:
        response = await chat_routes.chat_active_run(
            "chat-1",
            user=UserContext(
                user_id="user-1",
                user_center_id="center-1",
                username="tester",
            ),
            db=db,
        )

    replay_from = response["data"]["last_event_offset"]

    async def collect_existing_prefix():
        follower = executor.follow_run(row.run_id, from_offset=replay_from)
        try:
            return [await anext(follower), await anext(follower)]
        finally:
            await follower.aclose()

    events = await asyncio.wait_for(collect_existing_prefix(), timeout=1)
    assert replay_from == 0
    assert [event["_offset"] for event in events] == [1, 2]
    assert [event["delta"] for event in events] == [
        "already produced one",
        "already produced two",
    ]


@pytest.mark.asyncio
async def test_lease_heartbeat_fails_closed_when_renew_raises(monkeypatch):
    class ExplodingJournal:
        def renew(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(executor, "_journal", lambda: ExplodingJournal())
    monkeypatch.setattr(executor, "_RUN_LEASE_HEARTBEAT_SEC", 0.0)
    lease_lost = asyncio.Event()
    worker = asyncio.create_task(asyncio.Event().wait())
    heartbeat = asyncio.create_task(
        executor._lease_heartbeat("run-heartbeat", "worker", worker, lease_lost)
    )

    await asyncio.wait_for(heartbeat, timeout=1)
    await asyncio.sleep(0)
    assert lease_lost.is_set()
    assert worker.cancelled()


@pytest.mark.asyncio
async def test_periodic_recovery_retries_a_lease_that_was_live_at_startup(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-live-at-startup",
        message_id="msg-live-at-startup",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
            },
        },
    )
    assert journal.claim(row.run_id, owner="crashed-process", lease_seconds=60)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) + timedelta(milliseconds=60)}
        )
        db.commit()
    registered = asyncio.Event()

    def register(_run_id, coro, *, name):
        coro.close()
        registered.set()

    monkeypatch.setattr(executor, "_register_run_task", register)
    monkeypatch.setattr(executor, "_RUN_RECOVERY_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(executor, "_STALE_REAPER_INTERVAL_SEC", 60.0)
    loop_task = asyncio.create_task(executor.run_stale_reaper_loop())
    try:
        await asyncio.wait_for(registered.wait(), timeout=1)
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task


@pytest.mark.asyncio
async def test_reaper_terminal_cancels_worker_without_late_user_cancel_projection(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    entered = asyncio.Event()

    async def blocked_workflow(**_kwargs):
        entered.set()
        yield {"type": "model_progress"}
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "astream_chat_workflow", blocked_workflow)
    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    worker = executor._active_runs[run.run_id]
    hard_expired_at = datetime.now(timezone.utc) - timedelta(
        seconds=executor._HARD_MAX_AGE_SEC + 60
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == run.run_id).update(
            {"started_at": hard_expired_at, "created_at": hard_expired_at}
        )
        db.commit()

    assert await executor.reap_stale_runs() == 1
    await asyncio.wait_for(worker, timeout=1)
    entries = await redis.xrange(executor._stream_key(run.run_id), min="-", max="+")
    events = [json.loads(fields["data"]) for _entry_id, fields in entries]
    assert sum(event.get("type") == executor._TERMINAL_TYPE for event in events) == 1
    assert not any(event.get("_cancelled") for event in events)
    assert any("长时间无响应" in str(event.get("error") or "") for event in events)
    with sessions() as db:
        assert db.get(ChatRun, run.run_id).status == "failed"
