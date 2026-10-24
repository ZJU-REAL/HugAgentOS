"""Crash and ownership races for command completion."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.chat.runtime.handoff as handoff
import core.chat.runtime.recovery as recovery
import core.chat.runtime.worker as worker
import core.db.engine as engine
import core.runs.api_recovery as api_recovery
import core.runs.runtime as runs_runtime
import core.sandbox.commands.runtime as commands_runtime
from core.db import models
from core.sandbox.commands.runtime import CommandRuntime
from core.chat.sequencer import ChatSequencer
from core.sandbox.commands import delivery, store
from tests.orchestration.test_background_commands import command_db, request


def test_crash_before_context_injection_replays_durable_event(command_db, monkeypatch):
    from core.runs.journal import RecoveryDecision
    pass
    import core.runs.api_recovery as agent_api_recovery
    with command_db() as db:
        run = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={}).run
        run.recovery_snapshot = {"kind":"chat", "worker_args":{
            "session_messages":[], "effective_user_message":"start",
            "raw_user_message":"start", "context":{"chat_mode":"medium","enable_thinking":True}}}
        run_id = run.run_id
        db.commit()
    command = store.create(request(), "c", run_id)
    store.attach(command["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(command["task_id"], "owner", {"status":"exited","exit_code":0})
    delivery.reserve_boundary("c", "u", run_id)
    # Simulate death before append_context_text: resume only committed state.
    with command_db() as db:
        run = db.get(models.ChatRun, run_id)
        decision = RecoveryDecision(run_id=run_id, chat_id="c", user_id="u",
            message_id=run.message_id, action="resume", phase="running",
            snapshot=run.recovery_snapshot, request_payload={})
    captured = []
    async def nothing(): pass
    def workflow(**kwargs):
        captured.append(kwargs)
        return nothing()
    monkeypatch.setattr(engine, "SessionLocal", command_db)
    monkeypatch.setattr(worker, "_run_workflow", workflow)
    monkeypatch.setattr(runs_runtime, 'register_run_task', lambda rid, task, **kw: task.close())
    monkeypatch.setattr(api_recovery, "validate_recovery_context",
        lambda db, run, context, **kw: context)
    assert recovery._register_recovered_chat(decision)
    assert command["task_id"] in str(captured[0]["session_messages"])
    assert store.snapshot(command["task_id"])["notice_status"] == "delivered"

async def test_delayed_stop_renews_lease_and_cannot_be_recovered_twice(command_db, monkeypatch):
    import core.sandbox.commands.runtime as module
    monkeypatch.setattr(store, "LEASE_SECONDS", 1)
    stopping = False
    polls = 0
    async def poll(pid, **kwargs):
        nonlocal stopping, polls
        if kwargs["chars"]:
            stopping = True
        await asyncio.sleep(0.04)
        if stopping:
            polls += 1
        if polls >= 35:
            return {"status":"exited","exit_code":130}
        return {"status":"running","session_id":"p"}
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}), write_stdin=poll)
    recover = AsyncMock()
    monkeypatch.setattr(commands_runtime, "recover_execution", recover)
    owner, other = CommandRuntime(), CommandRuntime()
    result = await owner.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    cancel = asyncio.create_task(owner.cancel(result["task_id"], "u", "c"))
    while not cancel.done():
        await owner.recover(provider)
        await other.recover(provider)
        await asyncio.sleep(0.02)
    await cancel
    assert store.snapshot(result["task_id"])["status"] == "cancelled"
    recover.assert_not_awaited()
    assert provider.start_process.await_count == 1
    await owner.shutdown()
    await other.shutdown()

def test_command_result_retains_provider_output_contract(command_db):
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"exited","exit_code":0,
        "stdout":"done","output_files":{"stdout":"/workspace/log"},
        "execution_time_ms":123,"stdin_supported":False,"lifetime_note":"fixed TTL"}, None)
    result = store.read(row["task_id"], "u", "c")
    assert result["output_files"] == {"stdout":"/workspace/log"}
    assert result["execution_time_ms"] == 123
    assert result["stdin_supported"] is False
    assert result["lifetime_note"] == "fixed TTL"

def test_provider_omission_is_counted_once_for_each_reader(command_db):
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p",
        "output_omitted_chars":7}, None)
    assert store.read(row["task_id"], "u", "c")["output_omitted_chars"] == 7
    assert store.read(row["task_id"], "u", "c")["output_omitted_chars"] == 0
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0,
        "output_omitted_chars":3})
    assert store.read(row["task_id"], "u", "c")["output_omitted_chars"] == 3
    assert store.read(row["task_id"], "u", "c", consumer=None)["output_omitted_chars"] == 10

async def test_delete_during_launch_stops_unattached_process(command_db):
    launch = asyncio.Event()
    release = asyncio.Event()
    async def start(req, **kw):
        launch.set()
        await release.wait()
        return {"status":"running","session_id":"p"}
    provider = SimpleNamespace(name="test", start_process=start,
        write_stdin=AsyncMock(return_value={"status":"exited","exit_code":130}))
    runtime = CommandRuntime()
    pending = asyncio.create_task(runtime.start(provider, request(), chat_id="c",
        source_run_id=None, background=True))
    await launch.wait()
    with command_db() as db:
        db.query(models.CommandTask).delete()
        db.query(models.ChatSession).delete()
        db.commit()
    release.set()
    import pytest
    with pytest.raises(LookupError):
        await pending
    provider.write_stdin.assert_awaited_once()
    assert provider.write_stdin.call_args.kwargs["chars"] == chr(3)
    assert not runtime.tasks
    await runtime.shutdown()

def test_list_never_hides_live_tasks_behind_display_limit(command_db):
    for _ in range(51):
        store.create(request(), "c", None)
    rows = store.list_owned("u", "c")
    assert len(rows) == 51
    assert all(row["status"] == "starting" for row in rows)

async def test_slow_launch_keeps_lease_without_execution_deadline(command_db, monkeypatch):
    import core.sandbox.commands.runtime as module
    monkeypatch.setattr(store, "LAUNCH_LEASE_SECONDS", 0.2)
    monkeypatch.setattr(commands_runtime, "LAUNCH_RENEW_SECONDS", 0.03)
    called = asyncio.Event()
    async def start(req, **kwargs):
        called.set()
        await asyncio.sleep(0.6)
        return {"status":"exited","exit_code":0,"stdout":"once"}
    provider = SimpleNamespace(start_process=AsyncMock(side_effect=start))
    owner, competitor = CommandRuntime(), CommandRuntime()
    result = asyncio.create_task(owner.start(provider, request(), chat_id="c", source_run_id=None))
    await called.wait()
    while not result.done():
        await competitor.recover(provider)
        await asyncio.sleep(0.02)
    assert (await result)["stdout"] == "once"
    assert provider.start_process.await_count == 1
    await owner.shutdown()
    await competitor.shutdown()

def test_adoption_remains_idempotent_at_capacity(command_db):
    first = store.create(request(), "c", None, process_id="original")
    for _ in range(63):
        store.create(request(), "c", None)
    repeated = store.create(request(), "c", None, process_id="original")
    assert repeated["task_id"] == first["task_id"]
    assert not repeated["created"]

def test_steer_history_rebuild_preserves_command_sideband(command_db, monkeypatch):
    pass
    from core.chat.service import ChatService
    from core.sandbox.commands.context import append_active_completions
    with command_db() as db:
        run = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={}).run
        run.recovery_snapshot = {"kind":"chat", "worker_args":{
            "session_messages":[], "effective_user_message":"start",
            "raw_user_message":"start", "context":{"chat_mode":"medium","enable_thinking":True}}}
        run_id = run.run_id
        db.commit()
    command = store.create(request(), "c", run_id)
    store.attach(command["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(command["task_id"], "owner", {"status":"exited","exit_code":0})
    delivery.reserve_boundary("c", "u", run_id)
    event_id = "command_event_" + command["task_id"]
    # Use the actual history boundary called by _commit_steer, then replace the
    # snapshot exactly as steering does. Repeat to verify stable-event replay.
    with command_db() as db:
        service = ChatService(db)
        for _ in range(3):
            history = handoff._persisted_history(service, "c")
            assert sum(m.get("message_id") == event_id for m in history) == 1
            assert append_active_completions(db, "c", history) == history
            run = db.get(models.ChatRun, run_id)
            run.recovery_snapshot = {"kind":"chat", "worker_args":{
                "session_messages":history, "effective_user_message":"steer",
                "raw_user_message":"steer", "context":{"chat_mode":"medium","enable_thinking":True}}}
            db.commit()
        assert event_id in str(run.recovery_snapshot["worker_args"]["session_messages"])
        import core.runs.api_recovery as agent_api_recovery
        from core.runs.journal import RecoveryDecision
        decision = RecoveryDecision(run_id=run_id, chat_id="c", user_id="u",
            message_id=run.message_id, action="resume", phase="running",
            snapshot=run.recovery_snapshot, request_payload={})
        recovered_args = []
        async def nothing(): pass
        def workflow(**kwargs):
            recovered_args.append(kwargs)
            return nothing()
        monkeypatch.setattr(engine, "SessionLocal", command_db)
        monkeypatch.setattr(worker, "_run_workflow", workflow)
        monkeypatch.setattr(runs_runtime, 'register_run_task',
            lambda rid, task, **kw: task.close())
        monkeypatch.setattr(api_recovery, "validate_recovery_context",
            lambda db, run, context, **kw: context)
        assert recovery._register_recovered_chat(decision)
        assert sum(m.get("message_id") == event_id
                   for m in recovered_args[0]["session_messages"]) == 1
        # Active events must not become trailing persisted user turns.
        assert db.query(models.ChatMessage).filter_by(chat_id="c").count() == 2
        run.status, run.writer_slot = "completed", None
        db.commit()
        assert not any(m.get("message_id") == event_id
                       for m in handoff._persisted_history(service, "c"))


def test_command_sideband_does_not_cross_chat_or_user(command_db):
    from core.sandbox.commands.context import append_active_completions
    with command_db() as db:
        run = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={}).run
        run.recovery_snapshot = {"kind":"chat", "worker_args":{"session_messages":[],"context":{"chat_mode":"medium","enable_thinking":True}}}
        run_id = run.run_id
        db.commit()
    command = store.create(request(), "c", run_id)
    store.attach(command["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(command["task_id"], "owner", {"status":"exited","exit_code":0})
    delivery.reserve_boundary("c", "u", run_id)
    with command_db() as db:
        assert append_active_completions(db, "another-chat", []) == []
        run = db.get(models.ChatRun, run_id)
        run.user_id = "other"
        db.commit()
        assert append_active_completions(db, "c", []) == []


def test_active_completion_context_retains_harness_provenance():
    from core.llm.context.adapter import AgentScopeContextAdapter
    from core.llm.context.ir_constants import KIND_REMINDER, POLICY_NEVER, SESSION_CONTEXT_META_KEY
    from core.sandbox.commands.context import completion_context
    message = completion_context({"task_id":"cmd_event", "status":"completed", "exit_code":0})
    assert SESSION_CONTEXT_META_KEY in message
    assert message["extra_data"]["hidden_in_chat"] is True
    item, = AgentScopeContextAdapter().items_from_session_dict(message, created_seq=9)
    assert item.kind == KIND_REMINDER
    assert item.origin == "harness:command_completion"
    assert item.trust == "system"
    assert item.truncation_policy == POLICY_NEVER
    assert item.priority == 950


def test_idle_completion_message_retains_harness_provenance(command_db):
    from core.llm.context.adapter import AgentScopeContextAdapter
    from core.llm.context.ir_constants import KIND_REMINDER, POLICY_NEVER, SESSION_CONTEXT_META_KEY
    from core.chat.compaction.history import normalize_rows
    with command_db() as db:
        source = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={}).run
        source.recovery_snapshot = {"kind":"chat", "worker_args":{"session_messages":[],"context":{"chat_mode":"medium","enable_thinking":True}}}
        source.status, source.writer_slot = "completed", None
        run_id = source.run_id
        db.commit()
    command = store.create(request(), "c", run_id)
    store.attach(command["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(command["task_id"], "owner", {"status":"exited","exit_code":0})
    followup_id = delivery.prepare([store.snapshot(command["task_id"])], "dispatcher")
    with command_db() as db:
        followup = db.get(models.ChatRun, followup_id)
        row = db.get(models.ChatMessage, followup.user_message_id)
        assert SESSION_CONTEXT_META_KEY in row.extra_data
        message, = normalize_rows([row])
        item, = AgentScopeContextAdapter().items_from_session_dict(message, created_seq=9)
        assert item.kind == KIND_REMINDER
        assert item.origin == "harness:command_completion"
        assert item.trust == "system"
        assert item.truncation_policy == POLICY_NEVER
        assert item.priority == 950


async def test_observation_failure_preserves_cancellation_control(command_db):
    polls = 0
    async def poll(pid, **kwargs):
        nonlocal polls
        polls += 1
        if polls == 1:
            raise TimeoutError("transient output read")
        if kwargs["chars"]:
            return {"status": "exited", "exit_code": 130}
        return {"status": "running", "session_id": "p"}
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}),
        write_stdin=poll)
    owner = CommandRuntime()
    result = await owner.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    await asyncio.sleep(0.1)
    assert store.snapshot(result["task_id"])["status"] == "running"
    await owner.cancel(result["task_id"], "u", "c")
    assert store.snapshot(result["task_id"])["status"] == "cancelled"
    assert provider.start_process.await_count == 1
    await owner.shutdown()

async def test_failed_stop_keeps_shared_ownership_across_workers(command_db, monkeypatch):
    monkeypatch.setattr(store, "LEASE_SECONDS", 0.3)
    can_stop = False
    async def poll(pid, **kwargs):
        if can_stop and kwargs["chars"]:
            return {"status": "exited", "exit_code": 130}
        raise TimeoutError("process still alive; stop RPC unavailable")
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status": "running", "session_id": "p"}),
        write_stdin=poll)
    owner, other = CommandRuntime(), CommandRuntime()
    result = await owner.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    tid = result["task_id"]
    # Force the observer down its failed-stop path, retaining the process handle.
    task = owner.tasks[tid]
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for _ in range(20):
        await owner.recover(provider)
        await other.recover(provider)
        await asyncio.sleep(0.04)
    row = store.snapshot(tid)
    assert row["status"] == "running"
    assert row["owner"] == owner.owner
    assert tid in owner.providers
    can_stop = True
    await owner.recover(provider)
    await asyncio.sleep(0.1)
    assert store.snapshot(tid)["status"] == "failed"
    assert tid not in owner.providers
    await owner.shutdown()
    await other.shutdown()

async def test_lost_owner_must_stop_local_process_before_forgetting_handle(command_db):
    stopped = asyncio.Event()
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status": "running", "session_id": "p"}))
    async def poll(pid, **kwargs):
        assert kwargs["chars"] == chr(3)
        stopped.set()
        return {"status": "exited", "exit_code": 130}
    provider.write_stdin = poll
    owner = CommandRuntime()
    result = await owner.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    tid = result["task_id"]
    with command_db() as db:
        row = db.get(models.CommandTask, tid)
        row.owner, row.status = "successor", "unknown"
        db.commit()
    await asyncio.wait_for(stopped.wait(), 2)
    await asyncio.sleep(0.05)
    assert tid not in owner.providers
    await owner.shutdown()
