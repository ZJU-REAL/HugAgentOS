"""Delivery deadlines cannot rewrite business outcomes or start later sends."""
from tests.orchestration.automation_test_support import execute_queued

import asyncio
import threading
from types import SimpleNamespace

import pytest
from orchestration.automations import automation_delivery as module
from orchestration.automations.automation_scheduler import AutomationScheduler

pytest_plugins = ["tests.orchestration.test_automation_live_conversations"]


async def test_swallowed_notification_cancel_cannot_start_channel_delivery(monkeypatch):
    from integrations.channels import outbound

    monkeypatch.setattr(module, "DELIVERY_TIMEOUT_SECONDS", 0.01)
    resumed, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def notification(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await resumed.wait()
        stopped.set()

    async def send(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(outbound, "deliver_to_conversation", send)
    await module.deliver_result(
        SimpleNamespace(_send_notification=notification),
        "u",
        "t",
        "n",
        "success",
        "text",
        metadata={
            "delivery_targets": [
                {"type": "inapp"},
                {"type": "channel", "channel_id": "c", "conversation_id": "room"},
            ]
        },
    )
    resumed.set()
    await asyncio.wait_for(stopped.wait(), 1)
    await asyncio.sleep(0)
    assert calls == []


async def test_artifact_download_does_not_block_deadline_or_send_late(monkeypatch):
    from integrations.channels import outbound
    from orchestration.automations import automation_artifacts

    monkeypatch.setattr(module, "DELIVERY_TIMEOUT_SECONDS", 0.02)
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def load(chat_id):
        entered.set()
        assert finish.wait(2)
        return []

    async def send(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(automation_artifacts, "load_run_files", load)
    monkeypatch.setattr(outbound, "deliver_to_conversation", send)
    try:
        await module.deliver_result(
            SimpleNamespace(),
            "u",
            "t",
            "n",
            "success",
            "text",
            "chat",
            {
                "delivery_targets": [
                    {"type": "channel", "channel_id": "c", "conversation_id": "room"}
                ]
            },
        )
        assert entered.is_set()
        assert calls == []
    finally:
        finish.set()
    await asyncio.sleep(0.01)
    assert calls == []


async def test_delivery_error_keeps_committed_success(live_env, monkeypatch):
    from features.automation.service import AutomationService
    from orchestration.automations import automation_conversation

    factory, _, task_id = live_env

    async def prompt(**kwargs):
        return "chat", "answer", {}

    async def fail(*args):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(automation_conversation, "execute_prompt", prompt)
    scheduler = AutomationScheduler()
    monkeypatch.setattr(scheduler, "_send_notification", fail)
    await execute_queued(scheduler, task_id, "owner")
    with factory() as db:
        service = AutomationService(db)
        assert service.get_task_runs(task_id, "owner")[0].status == "success"
        assert service.get_task_by_id(task_id).consecutive_failures == 0
    assert not scheduler._leases


async def test_stale_executor_cannot_update_task_or_notify(live_env, monkeypatch):
    from features.automation.models import ScheduledTaskRun
    from features.automation.runs import AutomationRunRepository
    from features.automation.service import AutomationService
    from orchestration.automations import automation_conversation

    factory, _, task_id = live_env
    notifications = []

    async def prompt(**kwargs):
        with factory() as db:
            run = db.query(ScheduledTaskRun).filter_by(task_id=task_id).one()
            assert AutomationRunRepository(db).claim(
                run.run_id, "new-owner", expected_token=run.lease_token
            )
        return "chat", "stale answer", {}

    async def notify(*args):
        notifications.append(args)

    monkeypatch.setattr(automation_conversation, "execute_prompt", prompt)
    scheduler = AutomationScheduler()
    monkeypatch.setattr(scheduler, "_send_notification", notify)
    await execute_queued(scheduler, task_id, "owner")
    with factory() as db:
        service = AutomationService(db)
        run = service.get_task_runs(task_id, "owner")[0]
        assert run.status == "running"
        assert run.lease_token == "new-owner"
        assert run.result_summary is None
        assert service.get_task_by_id(task_id).last_run_at is None
    assert notifications == []


async def test_channel_exception_does_not_skip_other_targets(monkeypatch):
    from integrations.channels import outbound

    calls = []

    async def send(channel, *args, **kwargs):
        calls.append(channel)
        if channel == "bad":
            raise RuntimeError("adapter lookup failed")
        return True

    monkeypatch.setattr(outbound, "deliver_to_conversation", send)
    await module.deliver_result(
        SimpleNamespace(),
        "u",
        "t",
        "n",
        "success",
        "text",
        metadata={
            "delivery_targets": [
                {"type": "channel", "channel_id": name, "conversation_id": "room"}
                for name in ("bad", "good")
            ]
        },
    )
    assert calls == ["bad", "good"]
