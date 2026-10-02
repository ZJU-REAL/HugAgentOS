"""Behavioral regression coverage."""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from core.db.models import ChatMessage, ChatRun, ChatRunOperation, ChatSteerQueueItem
from core.services.run_journal import RunJournal
from core.services.steer_queue import SteerQueue
from orchestration import chat_run_executor as executor
from orchestration import run_event_stream

from tests.orchestration.recovery_test_support import (
    recovery_env,
    _async_none,
)


@pytest.mark.asyncio
async def test_automation_prompt_injects_durable_binding_and_recovery_surface(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    from core.db import engine as db_engine
    from core.services import ontology_service, user_model_selection
    from orchestration import workflow
    from orchestration.schedulers.automation_conversation import execute_prompt

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
    monkeypatch.setattr(executor, "astream_chat_workflow", bound_workflow)
    stream = run_event_stream.LocalRunEventStream()
    monkeypatch.setattr(executor, "get_run_event_stream", lambda: stream)
    from core.services.automation_service import AutomationService

    with sessions() as db:
        task = AutomationService(db).create_task(
            user_id="user-1",
            task_type="prompt",
            prompt="collect evidence",
            cron_expression="0 18 * * *",
        )
        task_id = task.task_id
    chat_id, text, _usage = await execute_prompt(
        user_id="user-1",
        task_name="nightly",
        prompt="collect evidence",
        task_id=task_id,
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
        yield {"type": "model_dispatch"}
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
