"""Long automation runs retain ownership without runtime-based recovery."""
from tests.orchestration.automation_test_support import execute_queued
from tests.features.automation.run_fixtures import running_run

import asyncio
from datetime import timedelta

import core.agent_runtime.runner as runner
import orchestration.automations.automation_lease as automations_automation_lease
import orchestration.automations.automation_lease as automation_lease
import pytest
from core.infra import ephemeral
from core.infra.time import utc_now
from features.automation.service import AutomationService
from orchestration.automations.automation_scheduler import REDIS_LOCK_PREFIX, AutomationScheduler

pytest_plugins = ["tests.orchestration.test_automation_live_conversations"]


@pytest.mark.asyncio
async def test_long_run_renews_across_many_lease_periods_and_excludes_other_workers(monkeypatch):
    # Advance the store clock instead of waiting for real minutes.
    clock = [100.0]
    monkeypatch.setattr(ephemeral.time, "time", lambda: clock[0])
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(automations_automation_lease, "RENEW_INTERVAL_SECONDS", 0.001)
    owner, other = AutomationScheduler(), AutomationScheduler()
    assert await owner._acquire_lock("long-task")
    try:
        for _ in range(100):
            clock[0] += 30
            await asyncio.sleep(0.003)
            assert not await other._acquire_lock("long-task")
        # More than 40 minutes elapsed without releasing execution ownership.
        assert clock[0] > 2500
    finally:
        await owner._release_lock("long-task")
    assert await other._acquire_lock("long-task")
    await other._release_lock("long-task")


@pytest.mark.asyncio
async def test_expired_owner_cannot_renew_or_delete_new_owners_lock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ephemeral.time, "time", lambda: clock[0])
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(automations_automation_lease, "RENEW_INTERVAL_SECONDS", 0.001)
    owner, other = AutomationScheduler(), AutomationScheduler()
    assert await owner._acquire_lock("task")
    old_lease = owner._leases["task"]
    clock[0] += 61
    assert await other._acquire_lock("task")
    await asyncio.wait_for(asyncio.shield(old_lease.renewal), 1)
    await owner._release_lock("task")
    assert await state.get(f"{REDIS_LOCK_PREFIX}task") == other._leases["task"].token
    await other._release_lock("task")


@pytest.mark.asyncio
async def test_lease_loss_cancels_inflight_execution(monkeypatch):
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(automations_automation_lease, "RENEW_INTERVAL_SECONDS", 0.001)
    lease = automation_lease.AutomationLease("test-loss")
    assert await lease.acquire()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    executing = asyncio.create_task(lease.run(work()))
    try:
        await entered.wait()
        await state.put(lease.key, "new-owner", ttl=60)
        with pytest.raises(RuntimeError, match="执行锁已失效"):
            await asyncio.wait_for(executing, 1)
        assert cancelled.is_set()
    finally:
        await lease.release()


@pytest.mark.asyncio
async def test_recovery_uses_ownership_instead_of_run_age(live_env):
    factory, _, task_id = live_env
    with factory() as db:
        svc = AutomationService(db)
        old = running_run(db, task_id)
        old.started_at = utc_now() - timedelta(days=3)
        db.commit()
    owner, recovering = AutomationScheduler(), AutomationScheduler()
    assert await owner._acquire_lock(task_id)
    try:
        await recovering._dispatch_pending()
        with factory() as db:
            assert AutomationService(db).get_task_runs(task_id, "owner")[0].status == "running"
    finally:
        await owner._release_lock(task_id)
    # Even a newly started orphan is recoverable once ownership is absent.
    with factory() as db:
        running_run(db, task_id)
    await recovering._dispatch_pending()
    await asyncio.gather(*recovering._executions)
    with factory() as db:
        runs = AutomationService(db).get_task_runs(task_id, "owner")
        assert len(runs) == 2
        assert all(run.status == "failed" for run in runs)


@pytest.mark.asyncio
async def test_missed_one_shot_cannot_bypass_another_workers_lock(live_env):
    factory, _, task_id = live_env
    with factory() as db:
        task = AutomationService(db).get_task(task_id, "owner")
        task.recurring = False
        task.schedule_type = "once"
        task.next_run_at = utc_now() - timedelta(minutes=1)
        db.commit()
    owner, recovering = AutomationScheduler(), AutomationScheduler()
    assert await owner._acquire_lock(task_id)
    try:
        await recovering._dispatch_pending()
        assert not recovering._executions
        with factory() as db:
            assert AutomationService(db).get_task_runs(task_id, "owner")[0].status == "pending"
    finally:
        await owner._release_lock(task_id)


@pytest.mark.asyncio
async def test_direct_execution_on_same_scheduler_cannot_share_ownership(live_env, monkeypatch):
    _, _, task_id = live_env
    scheduler = AutomationScheduler()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()

    monkeypatch.setattr('orchestration.automations.automation_dispatch.execute_task', dispatch)
    first = asyncio.create_task(execute_queued(scheduler, task_id, "owner"))
    try:
        await entered.wait()
        await execute_queued(scheduler, task_id, "owner")
        assert len(calls) == 1
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_orphan_recovery_observes_child_without_creating_duplicate(live_env, monkeypatch):
    factory, _, task_id = live_env
    pass
    from orchestration.automations.automation_conversation import execute_prompt

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def workflow(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        yield {"type": "content", "delta": "Recovered report"}
        yield {"type": "meta", "usage": {}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    with factory() as db:
        run_id = running_run(db, task_id).run_id
    child_observer = asyncio.create_task(
        execute_prompt(
            user_id="owner",
            task_name="Report",
            prompt="Research",
            task_id=task_id,
            enabled_mcp_ids=None,
            enabled_skill_ids=None,
            enabled_kb_ids=None,
            automation_run_id=run_id,
        )
    )
    scheduler = AutomationScheduler()

    async def noop(*args):
        pass

    monkeypatch.setattr(scheduler, "_send_notification", noop)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await scheduler._dispatch_pending()
        await asyncio.sleep(0.1)
        assert scheduler._executions
        await execute_queued(scheduler, task_id, "owner")
        assert len(calls) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*scheduler._executions), 5)
        await child_observer
        with factory() as db:
            runs = AutomationService(db).get_task_runs(task_id, "owner")
            assert len(runs) == 1
            assert runs[0].status == "success"
            assert runs[0].result_summary == "Recovered report"
    finally:
        release.set()
        await scheduler.stop()
        await asyncio.gather(child_observer, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_delivers_original_result_before_later_user_message(live_env, monkeypatch):
    factory, _, task_id = live_env
    from core.db.models import ChatMessage, ChatRun
    pass
    from orchestration.automations.automation_conversation import execute_prompt

    async def workflow(**kwargs):
        yield {"type": "content", "delta": "Scheduled report"}
        yield {"type": "meta", "usage": {}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    with factory() as db:
        run_id = running_run(db, task_id).run_id
    chat_id, _, _ = await execute_prompt(
        user_id="owner",
        task_name="Report",
        prompt="Research",
        task_id=task_id,
        enabled_mcp_ids=None,
        enabled_skill_ids=None,
        enabled_kb_ids=None,
        automation_run_id=run_id,
    )
    with factory() as db:
        db.add(
            ChatMessage(
                message_id="unrelated-message",
                chat_id=chat_id,
                role="assistant",
                content="Unrelated answer",
            )
        )
        db.add(
            ChatRun(
                run_id="unrelated-run",
                chat_id=chat_id,
                user_id="owner",
                message_id="unrelated-message",
                status="completed",
                request_payload={"kind": "chat"},
                created_at=utc_now(),
                recovery_snapshot={"message_id": "unrelated-message"},
            )
        )
        db.commit()
    scheduler = AutomationScheduler()
    notices = []

    async def notify(*args):
        notices.append(args)

    monkeypatch.setattr(scheduler, "_send_notification", notify)
    await scheduler._dispatch_pending()
    await asyncio.wait_for(asyncio.gather(*scheduler._executions), 5)
    with factory() as db:
        runs = AutomationService(db).get_task_runs(task_id, "owner")
        assert runs[0].result_summary == "Scheduled report"
    assert notices[0][4] == "Scheduled report"


@pytest.mark.asyncio
async def test_stop_releases_lease_even_before_execution_coroutine_starts(live_env):
    _, _, task_id = live_env
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock(task_id)
    lease = scheduler._leases[task_id]
    task = asyncio.create_task(scheduler.execute_task(task_id))
    scheduler._executions.add(task)
    task.add_done_callback(scheduler._executions.discard)
    await scheduler.stop()
    assert scheduler._leases == {}
    assert lease.renewal.done()
    assert await lease.state.get(lease.key) is None
