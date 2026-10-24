"""Scheduled runs expose the same live conversation boundary as interactive runs."""
from tests.orchestration.automation_test_support import execute_queued
from core.chat.projection.codec import chat_projection_codec

import asyncio

import core.agent_runtime.runner as runner
import core.chat.runtime.compaction as compaction
import core.chat.runtime.followups as followups
import core.db.engine as db_engine
import core.runs.events.store as store
import core.runs.events.store as run_event_stream
import core.runs.runtime as runtime
import pytest
from core.db.engine import Base
from features.automation.service import AutomationService
from core.chat.service import ChatService
from orchestration.automations.automation_scheduler import AutomationScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'live.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr(db_engine, "SessionLocal", factory)
    monkeypatch.setattr('core.chat.steer.SessionLocal', factory)
    monkeypatch.setattr(followups, "_spawn_followup_task", lambda **kw: None)
    monkeypatch.setattr(compaction, "_spawn_compaction_task", lambda **kw: None)
    stream = run_event_stream.LocalRunEventStream(chat_projection_codec())
    monkeypatch.setattr(store, "get_run_event_stream", lambda: stream)
    monkeypatch.setattr(
        'core.models.selection.resolve_effective_chat_model_name', lambda: "test-model"
    )
    monkeypatch.setattr(
        'core.ontology.runtime_policy.build_user_ontology_runtime', lambda **kw: (False, {})
    )
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    from core.infra import ephemeral

    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(
        'core.capabilities.catalog.resolver.resolve_all_runtime_enabled', lambda *args: ([], [], [])
    )
    monkeypatch.setattr("core.chat.context.resolve_all_runtime_enabled", lambda *args: ([], [], []))
    with factory() as db:
        task = AutomationService(db).create_task(
            user_id="owner",
            name="Daily report",
            task_type="prompt",
            prompt="Research",
            cron_expression="0 18 * * *",
        )
        task_id = task.task_id
    yield factory, stream, task_id
    engine.dispose()


@pytest.mark.asyncio
async def test_running_scheduled_conversation_can_replay_progress_and_be_stopped(
    live_env, monkeypatch
):
    factory, stream, task_id = live_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def workflow(**kwargs):
        yield {"type": "content", "delta": "Researching"}
        entered.set()
        await release.wait()
        yield {"type": "content", "delta": " complete"}
        yield {"type": "meta", "usage": {"total_tokens": 2}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    monkeypatch.setattr('core.agent_runtime.runner.astream_chat_workflow', workflow)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with factory() as db:
            history = AutomationService(db).get_task_runs(task_id, "owner")
            assert history[0].chat_id, "Running task must link its conversation immediately"
            chat_id = history[0].chat_id
        active = runtime.get_active_run_for_chat(chat_id, "owner")
        assert active is not None, "Running automation must be discoverable by normal chat recovery"
        assert runtime.get_active_run_for_chat(chat_id, "other") is None
        events = await stream.read(active.run_id)
        assert any(event.get("delta") == "Researching" for _, event in events)
        assert await stream.read(active.run_id) == events, "Navigation/refresh can replay progress"
        with pytest.raises(runtime.ChatRunPermissionDenied):
            await runtime.cancel_run(active.run_id, user_id="other")
        await runtime.cancel_run(active.run_id, user_id="owner")
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            service = AutomationService(db)
            run = service.get_task_runs(task_id, "owner")[0]
            assert run.status != "running"
            assert run.chat_id == chat_id
            assert service.get_task(task_id, "owner").consecutive_failures == 0
    finally:
        release.set()
        if not executing.done():
            await asyncio.wait_for(executing, 5)


@pytest.mark.asyncio
async def test_scheduled_prompt_accepts_instruction_and_delivers_final_segment(
    live_env, monkeypatch
):
    factory, stream, task_id = live_env
    from core.chat.steer import put_pending_steer, take_pending_steer

    entered, release = asyncio.Event(), asyncio.Event()

    async def workflow(*, context, **kwargs):
        yield {"type": "content", "delta": "Original draft"}
        entered.set()
        await release.wait()
        steer = await take_pending_steer(context["run_id"])
        assert steer["message"] == "Focus on risks"
        yield {"type": "steer_applied", **steer}
        yield {"type": "content", "delta": "Revised risk report"}
        yield {"type": "meta", "usage": {"total_tokens": 5}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    scheduler = AutomationScheduler()
    notices = []

    async def noop(*args):
        return None

    async def notify(*args):
        notices.append(args)

    monkeypatch.setattr(scheduler, "_send_notification", notify)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with factory() as db:
            chat_id = AutomationService(db).get_task_runs(task_id, "owner")[0].chat_id
        active = runtime.get_active_run_for_chat(chat_id, "owner")
        accepted = await put_pending_steer(
            active.run_id,
            {
                "chat_id": chat_id,
                "user_id": "owner",
                "steer_id": "instruction-1",
                "message": "Focus on risks",
            },
        )
        assert accepted["status"] == "accepted"
        release.set()
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            run = AutomationService(db).get_task_runs(task_id, "owner")[0]
            assert run.status == "success"
            assert run.result_summary == "Revised risk report"
            history, _, _ = ChatService(db).list_messages(chat_id, "owner")
            assert any(row.content == "Focus on risks" for row in history)
        assert notices[0][4] == "Revised risk report"
    finally:
        release.set()
        if not executing.done():
            await asyncio.wait_for(executing, 5)


@pytest.mark.asyncio
async def test_scheduler_waits_for_explicit_stop_without_execution_deadline(live_env, monkeypatch):
    factory, stream, task_id = live_env

    async def workflow(**kwargs):
        yield {"type": "content", "delta": "Work in progress"}
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    monkeypatch.setattr(
        'orchestration.automations.automation_scheduler.TASK_EXECUTION_TIMEOUT_S',
        0.3,
        raising=False,
    )
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await asyncio.sleep(0.6)
        assert not executing.done()
        with factory() as db:
            run = AutomationService(db).get_task_runs(task_id, "owner")[0]
            assert run.status == "running"
            active = runtime.get_active_run_for_chat(run.chat_id, "owner")
        await runtime.cancel_run(active.run_id, user_id="owner")
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            run = AutomationService(db).get_task_runs(task_id, "owner")[0]
            assert run.status == "failed"
            assert run.chat_id
            assert runtime.get_active_run_for_chat(run.chat_id, "owner") is None
    finally:
        if not executing.done():
            executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)


@pytest.mark.asyncio
async def test_scheduled_plan_exposes_steps_and_preserves_background_profile(live_env, monkeypatch):
    factory, stream, _ = live_env
    from core.chat.plans import PlanService

    entered, release = asyncio.Event(), asyncio.Event()
    observed = {}

    async def plan_workflow(**kwargs):
        observed.update(kwargs)
        yield {"type": "plan_step_start", "step_id": "step-1", "title": "Research"}
        entered.set()
        await release.wait()
        with factory() as db:
            PlanService(db).update_plan(kwargs["plan_id"], status="completed", completed_steps=1)
        yield {
            "type": "plan_complete",
            "result_text": "Plan report",
            "completed_steps": 1,
            "total_steps": 1,
            "usage": {"total_tokens": 3},
        }

    monkeypatch.setattr('orchestration.plans.mode.astream_execute_plan', plan_workflow)
    with factory() as db:
        plan = PlanService(db).create_plan(
            user_id="owner",
            title="Report",
            task_input="Research",
            steps=[{"title": "Research", "step_id": "step-1"}],
        )
        task = AutomationService(db).create_task(
            user_id="owner", task_type="plan", plan_id=plan.plan_id, cron_expression="0 18 * * *"
        )
        task_id = task.task_id
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with factory() as db:
            chat_id = AutomationService(db).get_task_runs(task_id, "owner")[0].chat_id
        active = runtime.get_active_run_for_chat(chat_id, "owner")
        assert active.request_payload["kind"] == "plan_execute"
        assert observed["automation_run"] is True
        assert any(evt["type"] == "plan_step_start" for _, evt in await stream.read(active.run_id))
        release.set()
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            run = AutomationService(db).get_task_runs(task_id, "owner")[0]
            assert run.status == "success" and run.result_summary == "Plan report"
    finally:
        release.set()
        if not executing.done():
            await asyncio.wait_for(executing, 5)


@pytest.mark.asyncio
async def test_scheduler_shutdown_and_admission_failure_release_locks(live_env, monkeypatch):
    _, _, task_id = live_env
    scheduler = AutomationScheduler()
    await scheduler.start()
    await scheduler.stop()
    assert not scheduler._running
    released = []

    original_unlock = scheduler._release_lock

    async def unlock(task_id):
        released.append(task_id)
        await original_unlock(task_id)

    monkeypatch.setattr(scheduler, "_release_lock", unlock)

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("features.automation.admission.admit", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await execute_queued(scheduler, task_id, "owner")
    assert released == []
    assert not scheduler._leases


@pytest.mark.asyncio
async def test_scheduled_plan_stop_settles_without_failure_increment(live_env, monkeypatch):
    factory, stream, _ = live_env
    from core.chat.plans import PlanService

    entered = asyncio.Event()

    async def plan_workflow(**kwargs):
        yield {"type": "plan_step_start", "step_id": "step-1", "title": "Research"}
        entered.set()
        while not runtime.is_run_cancelled(kwargs["run_id"]):
            await asyncio.sleep(0.01)
        with factory() as db:
            PlanService(db).update_plan(kwargs["plan_id"], status="cancelled")
        yield {"type": "plan_complete", "completed_steps": 0, "total_steps": 1}

    monkeypatch.setattr('orchestration.plans.mode.astream_execute_plan', plan_workflow)
    with factory() as db:
        plan = PlanService(db).create_plan(
            user_id="owner",
            title="Report",
            task_input="Research",
            steps=[{"title": "Research", "step_id": "step-1"}],
        )
        task = AutomationService(db).create_task(
            user_id="owner", task_type="plan", plan_id=plan.plan_id, cron_expression="0 18 * * *"
        )
        task_id = task.task_id
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    await asyncio.wait_for(entered.wait(), 5)
    with factory() as db:
        chat_id = AutomationService(db).get_task_runs(task_id, "owner")[0].chat_id
    active = runtime.get_active_run_for_chat(chat_id, "owner")
    await runtime.cancel_run(active.run_id, user_id="owner")
    await asyncio.wait_for(executing, 5)
    with factory() as db:
        svc = AutomationService(db)
        assert svc.get_task_runs(task_id, "owner")[0].status == "failed"
        assert svc.get_task(task_id, "owner").consecutive_failures == 0
    assert runtime.get_active_run_for_chat(chat_id, "owner") is None


@pytest.mark.asyncio
async def test_stopping_scheduler_preserves_durable_child_for_recovery(live_env, monkeypatch):
    factory, _, task_id = live_env
    entered = asyncio.Event()

    async def workflow(**kwargs):
        yield {"type": "content", "delta": "Working"}
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    from tests.orchestration.automation_test_support import request
    request(task_id, "owner")
    await scheduler._dispatch_pending()
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(scheduler.stop(), 5)
    assert not scheduler._executions
    with factory() as db:
        run = AutomationService(db).get_task_runs(task_id, "owner")[0]
        assert run.status == "running"
        assert run.chat_id
        active = runtime.get_active_run_for_chat(run.chat_id, "owner")
        assert active is not None
    await runtime.cancel_run(active.run_id, user_id="owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [True, False])
async def test_scheduled_tool_result_is_durable_before_next_model_round(
    live_env, monkeypatch, combined
):
    factory, stream, task_id = live_env
    from core.db.models import ChatMessage

    reached, release = asyncio.Event(), asyncio.Event()

    async def workflow(**kwargs):
        yield {"type": "content", "delta": "Browsing My Space"}
        yield {
            "type": "model_step",
            "step": {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "blocks": [
                    {
                        "type": "tool_call",
                        "id": "list-1",
                        "name": "space_list_myspace_files",
                        "input": "{}",
                    }
                ],
            },
        }
        yield {
            "type": "tool_call",
            "tool_id": "list-1",
            "tool_name": "space_list_myspace_files",
            "tool_args": {},
        }
        step = {
            "schema": "harness.steps.v1",
            "kind": "tool_result",
            "blocks": [
                {
                    "type": "tool_result",
                    "id": "list-1",
                    "name": "space_list_myspace_files",
                    "output": "Files listed",
                    "state": "success",
                }
            ],
        }
        result = {
            "type": "tool_result",
            "tool_id": "list-1",
            "tool_name": "space_list_myspace_files",
            "result": {"files": []},
        }
        if combined:
            result["model_step"] = step
        yield result
        if not combined:
            yield {"type": "model_step", "step": step}
        reached.set()
        await release.wait()
        yield {"type": "content", "delta": "; continuing classification"}
        yield {"type": "meta", "usage": {"total_tokens": 2}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    executing = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        with factory() as db:
            scheduled = AutomationService(db).get_task_runs(task_id, "owner")[0]
            active = runtime.get_active_run_for_chat(scheduled.chat_id, "owner")
            assert active is not None
            row = db.get(ChatMessage, active.message_id)
            assert row is not None
            assert row.model_steps[-1]["kind"] == "tool_result"
            assert row.tool_calls[0]["result"] == {"files": []}
            history, _, _ = ChatService(db).list_messages(scheduled.chat_id, "owner")
            assert sum(row.role == "user" for row in history) == 1
        events = await stream.read(active.run_id)
        assert any(event["type"] == "tool_result" for _, event in events)
        release.set()
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            run = AutomationService(db).get_task_runs(task_id, "owner")[0]
            assert run.status == "success"
            assert "continuing classification" in run.result_summary
    finally:
        release.set()
        if not executing.done():
            await asyncio.wait_for(executing, 5)
