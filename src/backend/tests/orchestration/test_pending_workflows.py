"""Restart between durable admission and local worker registration."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from core.db import engine, models
from core.runs import ownership, tasks, runtime
from orchestration import pending
from tests.orchestration.test_background_commands import command_db


@pytest.mark.parametrize("kind", ["plan_execute", "autonomous_loop"])
async def test_pending_admission_restarts_once_but_running_never_replays(command_db, monkeypatch, kind):
    monkeypatch.setattr(engine, "SessionLocal", command_db)
    real_dispatch = pending.dispatch_pending
    async def no_dispatch(run):
        pass
    monkeypatch.setattr(pending, "dispatch_pending", no_dispatch)
    if kind == "plan_execute":
        from orchestration.plans import admission, execute_run as module
        worker_name = "_run_plan_execute_workflow"
        run = await admission.start_plan_execute_run(
            plan_id="plan", chat_id="c", user_id="u",
            session_messages=[{"role": "user", "content": "original input"}],
            enabled_agent_ids=["analyst"], model_name="accepted-model",
        )
    else:
        from orchestration.loops import admission, run as module
        worker_name = "_run_autonomous_loop_workflow"
        run = await admission.start_autonomous_loop_run(
            loop_id="loop", chat_id="c", user_id="u",
            goal_spec={"goal": "original goal"}, budget={"max_iterations": 7},
            worker_max_iters=23, model_name="accepted-model",
        )
    executed = []
    async def worker(**args):
        if ownership.journal().claim(args["run_id"], owner="worker",
                                     lease_seconds=30, pending_only=True):
            executed.append(args)
    monkeypatch.setattr(module, worker_name, worker)
    monkeypatch.setattr(pending, "dispatch_pending", real_dispatch)
    # Fresh DB read represents the successor scheduler, without any local launch state.
    with command_db() as db:
        accepted = db.get(models.ChatRun, run.run_id)
        assert accepted.status == "pending"
        assert accepted.recovery_snapshot["initial_args"]["model_name"] == "accepted-model"
    accepted = runtime.get_run(run.run_id)
    assert await real_dispatch(accepted)
    await real_dispatch(accepted)
    await asyncio.sleep(0.05)
    assert len(executed) == 1
    if kind == "plan_execute":
        assert executed[0]["session_messages"][0]["content"] == "original input"
        assert executed[0]["enabled_agent_ids"] == ["analyst"]
    else:
        assert executed[0]["worker_max_iters"] == 23
        assert executed[0]["goal_spec"]["goal"] == "original goal"
    # Even a stale pending snapshot cannot replay an already-started execution.
    with command_db() as db:
        row = db.get(models.ChatRun, run.run_id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert not await real_dispatch(accepted)
    await asyncio.sleep(0.05)
    assert len(executed) == 1

    assert not ownership.journal().claim(run.run_id, owner="late-worker",
                                         lease_seconds=30, pending_only=True)
