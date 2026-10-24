"""Shared chat age caps do not apply to automation conversations or successors."""

from datetime import timedelta

import core.runs.watchdog as watchdog
import pytest
from core.db.models import ChatRun, ChatSession
from core.infra.time import utc_now

pytest_plugins = ["tests.orchestration.test_stale_reaper"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "plan_execute", "autonomous_loop"])
async def test_live_automation_beyond_shared_age_cap_survives(reaper_env, kind):
    factory, _ = reaper_env
    with factory() as db:
        db.add(
            ChatSession(
                chat_id="automation",
                user_id="owner",
                title="Report",
                extra_data={"automation_run": True},
            )
        )
        db.add(
            ChatRun(
                run_id="successor",
                chat_id="automation",
                user_id="owner",
                message_id="message",
                status="running",
                request_payload={"kind": kind},
                started_at=utc_now() - timedelta(days=3),
                created_at=utc_now() - timedelta(days=3),
                lease_owner="other-worker",
                lease_expires_at=utc_now() + timedelta(seconds=60),
            )
        )
        db.commit()
    assert await watchdog.reap_stale_runs() == 0
    with factory() as db:
        assert db.get(ChatRun, "successor").status == "running"
