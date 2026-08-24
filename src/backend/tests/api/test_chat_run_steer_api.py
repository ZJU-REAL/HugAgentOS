"""Route contract for durable steer/follow-up/next-run input."""

from types import SimpleNamespace

import pytest

from api.routes.v1 import chat_runs
from core.auth.backend import UserContext


def _user(user_id: str = "user-1") -> UserContext:
    return UserContext(user_id=user_id, user_center_id=user_id, username=user_id)


@pytest.mark.asyncio
async def test_followup_request_reaches_durable_adapter(monkeypatch):
    run = SimpleNamespace(
        run_id="run-1",
        chat_id="chat-1",
        user_id="user-1",
        status="running",
        request_payload={"kind": "chat"},
    )
    captured = {}

    async def accept(run_id, payload):
        captured.update(payload)
        return {
            "queue_id": "queue-1",
            "steer_id": payload["steer_id"],
            "run_id": run_id,
            "delivery_mode": "follow_up",
            "status": "accepted",
        }

    monkeypatch.setattr(chat_runs.chat_run_executor, "get_run", lambda _run_id: run)
    monkeypatch.setattr(chat_runs, "put_pending_steer", accept)

    result = await chat_runs.steer_chat_run(
        "run-1",
        chat_runs.SteerChatRunRequest(
            steer_id="steer-1",
            message="完成后继续",
            delivery_mode="followUp",
            replace_latest=False,
        ),
        _user(),
    )

    assert captured == {
        "steer_id": "steer-1",
        "message": "完成后继续",
        "run_id": "run-1",
        "chat_id": "chat-1",
        "user_id": "user-1",
        "delivery_mode": "followUp",
        "replace_latest": False,
    }
    assert result["data"]["queued"] is True
    assert result["data"]["status"] == "accepted"


@pytest.mark.asyncio
async def test_queue_status_query_is_owner_scoped(monkeypatch):
    run = SimpleNamespace(run_id="run-1", user_id="user-1")
    monkeypatch.setattr(chat_runs.chat_run_executor, "get_run", lambda _run_id: run)
    monkeypatch.setattr(
        chat_runs,
        "list_run_steers",
        lambda _run_id: [{"queue_id": "queue-1", "status": "claimed"}],
    )

    result = await chat_runs.list_chat_run_steers("run-1", _user())

    assert result["data"] == {
        "run_id": "run-1",
        "items": [{"queue_id": "queue-1", "status": "claimed"}],
    }
