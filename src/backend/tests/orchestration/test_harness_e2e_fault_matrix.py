"""Cross-module fault injections for the integrated durable Harness.

These tests intentionally exercise database seams shared by the run journal,
single-writer sequencer, durable steer queue, and Redis projection recovery.
They complement each module's focused crash-point tests with the interactions
that only exist after the Harness 4.1-4.9 branches are combined.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, ChatSteerQueueItem
from core.services import ChatService
from core.services.chat_sequencer import ChatBusyError, ChatSequencer
from core.services.run_journal import RunJournal, RunLeaseLost
from core.services.steer_queue import SteerQueue
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def harness_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'harness-fault-matrix.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="fault matrix"))
        db.commit()
    yield sessions
    engine.dispose()


def test_terminal_commit_atomically_transfers_writer_and_sequence(harness_sessions):
    sessions = harness_sessions
    with sessions() as db:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="first turn",
            request_payload={"kind": "chat"},
        )

    journal = RunJournal(sessions)
    owner = "fault-matrix-owner"
    assert journal.claim(accepted.run.run_id, owner=owner, lease_seconds=90)
    queued = SteerQueue(sessions).accept(
        target_run_id=accepted.run.run_id,
        chat_id="chat-1",
        user_id="user-1",
        steer_id="follow-after-complete",
        message="queued follow-up",
        delivery_mode="follow_up",
        replace_latest=False,
    )
    prepared: dict = {}

    def commit_output_and_handoff(db) -> None:
        ChatService(db).add_message(
            chat_id="chat-1",
            role="assistant",
            content="first answer",
            message_id=accepted.run.message_id,
            chat_seq=accepted.run.assistant_chat_seq,
            commit=False,
        )
        handoff = executor._commit_queued_handoff_in_session(
            db,
            source_run_id=accepted.run.run_id,
            chat_id="chat-1",
            user_id="user-1",
            session_messages=[{"role": "user", "content": "first turn"}],
            assistant_content="first answer",
            context={"chat_id": "chat-1", "user_id": "user-1"},
            model_name="test-model",
        )
        assert handoff is not None
        prepared.update(handoff)

    assert journal.complete(
        accepted.run.run_id,
        owner=owner,
        status="completed",
        commit_effect=commit_output_and_handoff,
    )

    with sessions() as db:
        source = db.get(ChatRun, accepted.run.run_id)
        successor = db.get(ChatRun, prepared["run_id"])
        queue_row = db.get(ChatSteerQueueItem, queued.queue_id)
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == "chat-1")
            .order_by(ChatMessage.chat_seq)
            .all()
        )
        assert (source.status, source.writer_slot) == ("completed", None)
        assert (successor.status, successor.writer_slot) == ("pending", "main")
        assert successor.assistant_chat_seq == 4
        assert queue_row.status == "applied"
        assert queue_row.applied_run_id == successor.run_id
        assert [(row.chat_seq, row.role, row.content) for row in messages] == [
            (1, "user", "first turn"),
            (2, "assistant", "first answer"),
            (3, "user", "queued follow-up"),
        ]
        with pytest.raises(ChatBusyError):
            ChatSequencer(db).accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="must remain fenced",
                request_payload={"kind": "chat"},
            )


@pytest.mark.asyncio
async def test_redis_projection_loss_keeps_db_recovery_and_fences_old_owner(
    harness_sessions,
):
    sessions = harness_sessions
    with sessions() as db:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="perform a tool effect",
            request_payload={"kind": "chat"},
        )
        row = db.get(ChatRun, accepted.run.run_id)
        row.recovery_snapshot = {
            "kind": "chat",
            "worker_args": {
                "session_messages": [],
                "effective_user_message": "perform a tool effect",
                "raw_user_message": "perform a tool effect",
                "context": {},
            },
        }
        row.snapshot_version = 1
        row.run_phase = "accepted"
        db.commit()

    queue = SteerQueue(sessions)
    queued = queue.accept(
        target_run_id=accepted.run.run_id,
        chat_id="chat-1",
        user_id="user-1",
        steer_id="db-survives-redis",
        message="durable instruction",
        replace_latest=False,
    )
    journal = RunJournal(sessions)
    owner = "crashed-owner"
    assert journal.claim(accepted.run.run_id, owner=owner, lease_seconds=90)
    journal.append_operation(
        accepted.run.run_id,
        owner=owner,
        operation_type="tool_result_unknown",
        phase="tool_result_unknown",
        safety="unknown_side_effect",
        payload={"effect_id": "effect-1"},
    )

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.xadd(
        executor._stream_key(accepted.run.run_id),
        {"data": '{"type":"partial"}'},
    )
    await redis.flushall()
    assert await redis.exists(executor._stream_key(accepted.run.run_id)) == 0

    with sessions() as db:
        row = db.get(ChatRun, accepted.run.run_id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    decisions = journal.recover()
    assert [(item.run_id, item.action) for item in decisions] == [
        (accepted.run.run_id, "needs_attention")
    ]
    with sessions() as db:
        recovered = db.get(ChatRun, accepted.run.run_id)
        durable_queue = db.get(ChatSteerQueueItem, queued.queue_id)
        assert (recovered.status, recovered.writer_slot) == ("needs_attention", None)
        assert durable_queue.status == "accepted"
        assert durable_queue.message == "durable instruction"

    with pytest.raises(RunLeaseLost):
        journal.append_operation(
            accepted.run.run_id,
            owner=owner,
            operation_type="late_write",
            phase="message_committed",
            safety="side_effect_committed",
        )
