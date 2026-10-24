"""Admission cleanup for scheduled conversations."""
from tests.orchestration.automation_test_support import execute_queued

import asyncio

import core.agent_runtime.runner as runner
import core.chat.runtime.admission as admission
import core.runs.runtime as runtime
import pytest
from core.db.models import ChatRun
from features.automation.service import AutomationService
from orchestration.automations.automation_scheduler import AutomationScheduler

pytest_plugins = ["tests.orchestration.test_automation_live_conversations"]


@pytest.mark.asyncio
async def test_failed_launch_releases_the_accepted_writer(live_env, monkeypatch):
    factory, _, task_id = live_env
    accepted_id = None

    async def fail_launch(*, accepted_run, **kwargs):
        nonlocal accepted_id
        accepted_id = accepted_run.run_id
        raise RuntimeError("launch unavailable")

    monkeypatch.setattr(admission, "start_run", fail_launch)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    await execute_queued(scheduler, task_id, "owner")
    with factory() as db:
        run = db.get(ChatRun, accepted_id)
        assert run.status == "failed"
        assert run.writer_slot is None
        scheduled = AutomationService(db).get_task_runs(task_id, "owner")[0]
        assert scheduled.status == "failed"
        assert scheduled.chat_id == run.chat_id
        assert runtime.get_active_run_for_chat(run.chat_id, "owner") is None


@pytest.mark.asyncio
async def test_cancelled_launch_releases_the_accepted_writer(live_env, monkeypatch):
    factory, _, task_id = live_env
    accepted_id = None

    async def cancelled_launch(*, accepted_run, **kwargs):
        nonlocal accepted_id
        accepted_id = accepted_run.run_id
        raise asyncio.CancelledError()

    monkeypatch.setattr(admission, "start_run", cancelled_launch)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    with pytest.raises(asyncio.CancelledError):
        await execute_queued(scheduler, task_id, "owner")
    with factory() as db:
        run = db.get(ChatRun, accepted_id)
        assert run.status == "failed"
        assert run.writer_slot is None


@pytest.mark.asyncio
async def test_scheduler_passes_explicit_agents_through_main_runtime_context(live_env, monkeypatch):
    factory, _, task_id = live_env
    captured = {}

    async def workflow(*, context, **kwargs):
        captured.update(context)
        yield {"type": "content", "delta": "Complete"}
        yield {"type": "meta", "usage": {}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    with factory() as db:
        AutomationService(db).update_task(
            task_id,
            "owner",
            enabled_mcp_ids=["search"],
            enabled_skill_ids=["research"],
            enabled_agent_ids=["analyst"],
        )
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    await execute_queued(scheduler, task_id, "owner")
    assert captured["enabled_agents"] == ["analyst"]
    assert captured["enabled_skills"] == ["research"]
    assert captured["enabled_mcps"] == ["search"]
    assert captured["automation_run"] is True
    assert captured["chat_mode"] == "medium"
    assert captured["enable_thinking"] is True
    with factory() as db:
        scheduled = AutomationService(db).get_task_runs(task_id, "owner")[0]
        payload = (
            db.query(ChatRun).filter(ChatRun.chat_id == scheduled.chat_id).one().request_payload
        )
        assert payload["chat_mode"] == captured["chat_mode"]
        assert payload["enable_thinking"] == captured["enable_thinking"]
    assert captured["run_id"] and captured["message_id"]


async def test_child_acceptance_already_contains_parent_link_and_recovery(live_env, monkeypatch):
    from core.db.models import ChatRun
    from features.automation.models import ScheduledTaskRun
    from orchestration.automations import automation_conversation

    factory, _, task_id = live_env
    observed = []

    async def crash_before_launch(**kwargs):
        child_id = kwargs["accepted_run"].run_id
        with factory() as db:
            child = db.get(ChatRun, child_id)
            parent = db.query(ScheduledTaskRun).filter_by(task_id=task_id).one()
            assert child.status == "pending"
            assert parent.chat_id == child.chat_id
            assert child.recovery_snapshot["kind"] == "chat"
            assert child.recovery_snapshot["worker_args"]["effective_user_message"] == "Research"
            assert child.recovery_snapshot["worker_args"]["context"]["automation_run"] is True
            observed.append(child_id)
        raise RuntimeError("worker died after durable acceptance")

    monkeypatch.setattr(automation_conversation.admission, "start_run", crash_before_launch)
    await execute_queued(AutomationScheduler(), task_id, "owner")
    assert len(observed) == 1
