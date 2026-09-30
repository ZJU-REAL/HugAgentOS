"""Late user instructions remain part of a scheduled conversation's outcome."""

import asyncio
import pytest

from core.services.automation_service import AutomationService
from core.services.steer_queue import SteerQueue
from orchestration import chat_run_executor as executor
from orchestration.schedulers.automation_scheduler import AutomationScheduler

pytest_plugins = ["tests.orchestration.test_automation_live_conversations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "stop", "timeout"])
async def test_scheduled_outcome_follows_the_main_executor_handoff(live_env, monkeypatch, outcome):
    factory, _, task_id = live_env
    entered, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def workflow(*, context, **kwargs):
        runs.append(context["run_id"])
        if len(runs) == 1:
            yield {"type": "content", "delta": "Original"}
            # Accepted after the last workflow boundary: the main executor
            # must carry this instruction to its existing successor mechanism.
            SteerQueue(factory).accept(
                target_run_id=context["run_id"],
                chat_id=context["chat_id"],
                user_id="owner",
                steer_id="late-instruction",
                message="Revised",
            )
        else:
            entered.set()
            await release.wait()
            yield {"type": "content", "delta": "Revised final result"}
        yield {"type": "meta", "usage": {}}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_release_lock", noop)
    monkeypatch.setattr(scheduler, "_send_notification", noop)
    if outcome == "timeout":
        monkeypatch.setattr(
            "orchestration.schedulers.automation_scheduler.TASK_EXECUTION_TIMEOUT_S", 1
        )
    executing = asyncio.create_task(scheduler.execute_task(task_id, "owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.2)
        assert not executing.done(), "The original result must not settle the scheduled task"
        assert executor.get_successor_run(runs[0], user_id="owner").run_id == runs[1]
        with factory() as db:
            assert AutomationService(db).get_task_runs(task_id, "owner")[0].status == "running"
        if outcome == "complete":
            release.set()
        elif outcome == "stop":
            await executor.cancel_run(runs[1], user_id="owner")
        await asyncio.wait_for(executing, 5)
        with factory() as db:
            scheduled = AutomationService(db).get_task_runs(task_id, "owner")[0]
            if outcome == "complete":
                assert scheduled.status == "success"
                assert scheduled.result_summary == "Revised final result"
            else:
                assert scheduled.status == "failed"
                assert executor.get_run(runs[1]).status == "cancelled"
                assert executor.get_active_run_for_chat(scheduled.chat_id, "owner") is None
    finally:
        release.set()
        if not executing.done():
            executing.cancel()
            await asyncio.gather(executing, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_successor_when_completion_wins_cancel_race(live_env, monkeypatch):
    factory, _, task_id = live_env
    source_entered, finish_source = asyncio.Event(), asyncio.Event()
    successor_entered, finish_successor = asyncio.Event(), asyncio.Event()
    runs, cancelled = [], []
    original_cancel = executor.cancel_run

    async def workflow(*, context, **kwargs):
        runs.append(context["run_id"])
        if len(runs) == 1:
            yield {"type": "content", "delta": "Original"}
            SteerQueue(factory).accept(
                target_run_id=context["run_id"],
                chat_id=context["chat_id"],
                user_id="owner",
                steer_id="race-instruction",
                message="Continue",
            )
            source_entered.set()
            await finish_source.wait()
        else:
            successor_entered.set()
            await finish_successor.wait()
        yield {"type": "meta", "usage": {}}

    async def completion_wins_cancel(run_id, *, user_id):
        cancelled.append(run_id)
        if run_id == runs[0]:
            # The observer entered cancellation before the original completed.
            # Commit its successor before the cancellation CAS can fence it.
            finish_source.set()
            await asyncio.wait_for(successor_entered.wait(), 5)
        return await original_cancel(run_id, user_id=user_id)

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "cancel_run", completion_wins_cancel)
    scheduler = AutomationScheduler()

    async def noop(*args):
        return None

    monkeypatch.setattr(scheduler, "_release_lock", noop)
    executing = asyncio.create_task(scheduler.execute_task(task_id, "owner"))
    try:
        await asyncio.wait_for(source_entered.wait(), 5)
        executing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(executing, 5)
        assert cancelled == runs
        assert executor.get_run(runs[1]).status == "cancelled"
        assert executor.get_active_run_for_chat(executor.get_run(runs[0]).chat_id, "owner") is None
    finally:
        finish_source.set()
        finish_successor.set()
        if not executing.done():
            executing.cancel()
            await asyncio.gather(executing, return_exceptions=True)
