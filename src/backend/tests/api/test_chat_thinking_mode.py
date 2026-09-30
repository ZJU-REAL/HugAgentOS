"""Resume probes use the same default thinking mode as main agent execution."""

from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from api.routes.v1 import chats
from orchestration import chat_run_executor as executor


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, True),
        ({"chat_mode": "medium"}, True),
        ({"chat_mode": "fast"}, False),
        ({"enable_thinking": False}, False),
    ],
)
def test_active_run_probe_preserves_the_agent_mode(monkeypatch, payload, expected):
    run = SimpleNamespace(
        run_id="run-test",
        message_id="message-test",
        status="running",
        started_at=datetime.now(timezone.utc),
        request_payload=payload,
    )
    monkeypatch.setattr(executor, "get_active_run_for_chat", lambda *args: run)
    monkeypatch.setattr(chats, "resolve_db_user_id", lambda db, user_id: user_id)
    response = chats.chat_active_run(
        "chat-test", user=SimpleNamespace(user_id="owner"), db=object()
    )
    assert response["data"]["enable_thinking"] is expected
