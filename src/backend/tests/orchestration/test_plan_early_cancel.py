"""Cancellation before Plan streaming must still settle partial persistence."""

import asyncio
from types import SimpleNamespace

import pytest
from orchestration.plans import execute_run


@pytest.mark.asyncio
async def test_user_cancel_at_first_event_persists_interrupted_message(monkeypatch):
    messages = []
    committed = []
    expired = []
    journal = SimpleNamespace(claim=lambda *a, **k: True)

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args):
            return None

        def commit(self):
            committed.append(True)

    class Offsets:
        last = 0

        def __init__(self, *args, lease_lost, **kwargs):
            self.lease_lost = lease_lost

        async def emit(self, event, **kwargs):
            self.lease_lost.set()
            raise asyncio.CancelledError

    async def heartbeat(*args):
        await asyncio.Event().wait()

    async def expire(run_id):
        expired.append(run_id)

    from core.llm.runtime import workspace
    from core.files.artifacts import service as artifact_service
    from core.projects import edition as project_scope

    monkeypatch.setattr(execute_run.ownership, "journal", lambda: journal)
    monkeypatch.setattr(execute_run.ownership, "cancelled_by_user", lambda _: True)
    monkeypatch.setattr(execute_run.ownership, "lease_heartbeat", heartbeat)
    monkeypatch.setattr(execute_run.writer, "EventOffsets", Offsets)
    monkeypatch.setattr(execute_run.writer, "expire_stream", expire)
    monkeypatch.setattr(execute_run.engine, "SessionLocal", DB)
    monkeypatch.setattr(
        "core.chat.service.ChatService",
        lambda db: SimpleNamespace(add_message=lambda **kwargs: messages.append(kwargs)),
    )
    monkeypatch.setattr(artifact_service, "persist_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(project_scope, "project_scope_from_chat_id", lambda *a: None)
    monkeypatch.setattr(workspace, "init_state", lambda: None)
    monkeypatch.setattr(workspace, "get_pinned", lambda: [])
    monkeypatch.setattr(workspace, "get_pinned_file_ids", lambda: [])

    await execute_run._run_plan_execute_workflow(
        run_id="run",
        plan_id="plan",
        chat_id="chat",
        user_id="user",
        message_id="message",
        enabled_mcp_ids=[],
        enabled_skill_ids=[],
        enabled_kb_ids=[],
        enabled_agent_ids=[],
        session_messages=[],
        model_name="test",
        journal_owner="owner",
    )
    assert len(messages) == 1
    assert "已中断" in messages[0]["content"]
    assert messages[0]["extra_data"]["plan_snapshot"]["cancelled"] is True
    assert committed == [True]
    assert expired == ["run"]
