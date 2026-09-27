"""Copied chat history never becomes fresh usage or evolution evidence."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from core.db.models import ChatMessage
from tests.api.chat_fork_fixtures import fork_env  # noqa: F401


@pytest.mark.parametrize("group_by", ["user", "model", "day"])
def test_security_usage_overview_ignores_forked_history(fork_env, group_by):
    from api.routes.v1.internal_security import SecurityQueryBody, usage

    client, sessions, _ = fork_env
    moment = datetime.utcnow() - timedelta(hours=1)
    with sessions() as db:
        for row in db.query(ChatMessage):
            row.created_at = moment
        db.get(ChatMessage, "a1").error = {"error": "Recorded model failure"}
        db.commit()
    body = SecurityQueryBody(
        group_by=group_by,
        start_time=(moment - timedelta(minutes=10)).isoformat(),
        end_time=(moment + timedelta(minutes=10)).isoformat(),
    )
    with sessions() as db:
        before = usage(body, db)
    assert sum(item["total_requests"] for item in before["items"]) == 2
    response = client.post(
        "/v1/chats/source/fork",
        json={"request_id": str(uuid4()), "through_message_id": "a1"},
    )
    assert response.status_code == 201
    with sessions() as db:
        assert usage(body, db) == before


def test_scheduled_backfill_does_not_create_episodes_for_forked_history(fork_env, monkeypatch):
    from core.db.models import ToolCallLog
    from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
    from core.evolution import backfill, trace_assembler, trace_store

    client, sessions, _ = fork_env
    with sessions() as db:
        for model in (ToolCallLog, EvolutionEpisode, EvolutionTraceEvent):
            model.__table__.create(db.get_bind())
    for module in (backfill, trace_assembler, trace_store):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    # Sanitization and unrelated execution log tables have their own tests.
    monkeypatch.setattr(trace_assembler, "_preview", lambda text: text)
    monkeypatch.setattr(trace_assembler, "_count_existing_logs", lambda db, mid: {})
    response = client.post("/v1/chats/source/fork", json={"request_id": str(uuid4())})
    assert response.status_code == 201
    first = backfill.backfill_episodes(max_batches=3)
    assert first["scanned"] == 2
    assert first["created"] == 2
    with sessions() as db:
        assert {row.message_id for row in db.query(EvolutionEpisode)} == {"a1", "a2"}
    second = backfill.backfill_episodes(max_batches=3)
    assert second["scanned"] == second["created"] == 0
