"""Durable steer/follow-up/next-run queue contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, ChatSteerQueueItem
from core.services.run_journal import RunJournal
from core.services.steer_queue import SteerClaimLost, SteerQueue, SteerQueueConflict
from orchestration.chat_run_executor import _commit_queued_handoff_in_session


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture()
def queue_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'steer-queue.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    clock = Clock()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    run = RunJournal(sessions, clock=clock).accept(
        run_id="run-1",
        message_id="msg-1",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    yield SteerQueue(sessions, clock=clock), sessions, clock, run
    engine.dispose()


def _accept(queue: SteerQueue, steer_id: str, **kwargs):
    return queue.accept(
        target_run_id="run-1",
        chat_id="chat-1",
        user_id="user-1",
        steer_id=steer_id,
        message=f"instruction {steer_id}",
        **kwargs,
    )


def test_three_instructions_receive_monotonic_chat_sequence(queue_env):
    queue, _sessions, _clock, _run = queue_env

    rows = [_accept(queue, f"s{index}", replace_latest=False) for index in range(3)]

    assert [row.steer_seq for row in rows] == [1, 2, 3]
    assert [row.steer_id for row in queue.list_for_run("run-1")] == ["s0", "s1", "s2"]


def test_concurrent_accepts_allocate_unique_monotonic_sequence(queue_env):
    queue, _sessions, _clock, _run = queue_env
    barrier = Barrier(3)

    def accept(index: int):
        barrier.wait(timeout=5)
        return _accept(queue, f"concurrent-{index}", replace_latest=False)

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(accept, range(3)))

    assert sorted(row.steer_seq for row in rows) == [1, 2, 3]
    assert len({row.queue_id for row in rows}) == 3


def test_replace_latest_keeps_superseded_audit_row(queue_env):
    queue, _sessions, _clock, _run = queue_env
    old = _accept(queue, "old")
    latest = _accept(queue, "latest")

    rows = queue.list_for_run("run-1")

    assert old.queue_id != latest.queue_id
    assert [(row.steer_id, row.status) for row in rows] == [
        ("old", "superseded"),
        ("latest", "accepted"),
    ]
    assert rows[0].superseded_by == latest.queue_id


def test_same_id_is_idempotent_only_for_the_exact_same_semantics(queue_env):
    queue, sessions, _clock, _run = queue_env
    original = _accept(queue, "stable-id", replace_latest=False)
    retry = _accept(queue, "stable-id", replace_latest=False)

    assert retry.queue_id == original.queue_id
    with pytest.raises(SteerQueueConflict):
        queue.accept(
            target_run_id="run-1",
            chat_id="chat-1",
            user_id="user-1",
            steer_id="stable-id",
            message="edited after a lost response",
            replace_latest=False,
        )
    with pytest.raises(SteerQueueConflict):
        queue.accept(
            target_run_id="run-1",
            chat_id="chat-1",
            user_id="user-1",
            steer_id="stable-id",
            message="instruction stable-id",
            delivery_mode="follow_up",
            replace_latest=False,
        )
    RunJournal(sessions).accept(
        run_id="run-2",
        message_id="msg-2",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    with pytest.raises(SteerQueueConflict):
        queue.accept(
            target_run_id="run-2",
            chat_id="chat-1",
            user_id="user-1",
            steer_id="stable-id",
            message="instruction stable-id",
            replace_latest=False,
        )


def test_claimed_item_is_redelivered_after_lease_expiry_and_applied_once(queue_env):
    queue, sessions, clock, _run = queue_env
    accepted = _accept(queue, "lease", replace_latest=False)
    first = queue.claim_next("run-1", owner="worker-a", lease_seconds=10)
    assert first.queue_id == accepted.queue_id
    assert queue.claim_next("run-1", owner="worker-b", lease_seconds=10) is None

    clock.advance(11)
    second = queue.claim_next("run-1", owner="worker-b", lease_seconds=10)
    assert second.queue_id == accepted.queue_id
    assert second.delivery_attempt == 2

    with sessions() as db:
        assert queue.mark_applied_in_session(
            db,
            queue_id=accepted.queue_id,
            claim_owner="worker-b",
            applied_run_id="run-1",
            applied_operation_seq=4,
        )
        db.commit()
    with sessions() as db:
        row = db.get(ChatSteerQueueItem, accepted.queue_id)
        assert row.status == "applied"
        assert row.applied_source_run_id == "run-1"
        assert row.applied_operation_seq == 4

    with sessions() as db:
        with pytest.raises(SteerClaimLost):
            queue.mark_applied_in_session(
                db,
                queue_id=accepted.queue_id,
                claim_owner="worker-a",
                applied_run_id="run-1",
                applied_operation_seq=5,
            )


def test_delivery_modes_are_ordered_but_only_steer_is_claimed_mid_run(queue_env):
    queue, _sessions, _clock, _run = queue_env
    steer = _accept(queue, "steer", delivery_mode="steer", replace_latest=False)
    follow_up = _accept(queue, "follow", delivery_mode="follow_up", replace_latest=False)
    next_run = _accept(queue, "next", delivery_mode="next_run", replace_latest=False)

    claimed = queue.claim_next("run-1", owner="worker", lease_seconds=30)

    assert claimed.queue_id == steer.queue_id
    assert [row.queue_id for row in queue.list_for_run("run-1")] == [
        steer.queue_id,
        follow_up.queue_id,
        next_run.queue_id,
    ]


def test_next_run_cannot_be_orphaned_after_target_completion(queue_env):
    queue, sessions, _clock, _run = queue_env
    journal = RunJournal(sessions)
    assert journal.claim("run-1", owner="finisher", lease_seconds=90)
    assert journal.complete("run-1", owner="finisher", status="completed")

    with pytest.raises(SteerQueueConflict):
        _accept(queue, "too-late", delivery_mode="next_run", replace_latest=False)
    with pytest.raises(SteerQueueConflict):
        queue.accept(
            target_run_id=None,
            chat_id="chat-1",
            user_id="user-1",
            steer_id="unbound-next-run",
            message="must have a completion trigger",
            delivery_mode="next_run",
            replace_latest=False,
        )
    assert queue.list_for_run("run-1") == []


def test_handoff_item_has_single_database_winner(queue_env):
    queue, sessions, _clock, _run = queue_env
    item = _accept(queue, "follow", delivery_mode="follow_up", replace_latest=False)
    barrier = Barrier(2)

    def consume(run_id: str):
        barrier.wait(timeout=5)
        with sessions() as db:
            selected = SteerQueue.consume_next_handoff_in_session(
                db,
                source_run_id="run-1",
                chat_id="chat-1",
                applied_run_id=run_id,
            )
            db.commit()
            return selected.queue_id if selected else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(consume, ("run-next-a", "run-next-b")))

    assert winners.count(item.queue_id) == 1
    assert winners.count(None) == 1
    with sessions() as db:
        applied = db.get(ChatSteerQueueItem, item.queue_id)
        assert applied.status == "applied"
        assert applied.applied_run_id in {"run-next-a", "run-next-b"}


def test_multiple_followups_stay_on_the_same_handoff_chain(queue_env):
    queue, sessions, _clock, _run = queue_env
    first = _accept(queue, "follow-1", delivery_mode="follow_up", replace_latest=False)
    second = _accept(queue, "follow-2", delivery_mode="follow_up", replace_latest=False)

    with sessions() as db:
        consumed_first = SteerQueue.consume_next_handoff_in_session(
            db,
            source_run_id="run-1",
            chat_id="chat-1",
            applied_run_id="run-next-1",
        )
        db.commit()
    with sessions() as db:
        consumed_second = SteerQueue.consume_next_handoff_in_session(
            db,
            source_run_id="run-next-1",
            root_run_id="run-1",
            chat_id="chat-1",
            applied_run_id="run-next-2",
        )
        db.commit()

    assert consumed_first.queue_id == first.queue_id
    assert consumed_first.applied_source_run_id == "run-1"
    assert consumed_second.queue_id == second.queue_id
    assert consumed_second.applied_source_run_id == "run-next-1"
    assert [item.queue_id for item in queue.list_for_run("run-next-1")] == [
        second.queue_id
    ]


def test_cancel_preserves_row_and_prevents_delivery(queue_env):
    queue, sessions, _clock, _run = queue_env
    item = _accept(queue, "cancel", replace_latest=False)

    assert queue.cancel("run-1", "cancel") is True
    assert queue.claim_next("run-1", owner="worker", lease_seconds=30) is None
    with sessions() as db:
        row = db.get(ChatSteerQueueItem, item.queue_id)
        assert row.status == "cancelled"


def test_expired_claim_can_be_cancelled_without_racing_a_live_worker(queue_env):
    queue, sessions, clock, _run = queue_env
    item = _accept(queue, "cancel-expired", replace_latest=False)
    assert queue.claim_next("run-1", owner="dead-worker", lease_seconds=10)

    assert queue.cancel("run-1", "cancel-expired") is False
    clock.advance(11)
    assert queue.cancel("run-1", "cancel-expired") is True
    assert queue.claim_next("run-1", owner="replacement", lease_seconds=10) is None
    with sessions() as db:
        row = db.get(ChatSteerQueueItem, item.queue_id)
        assert row.status == "cancelled"


def test_run_completion_atomically_creates_exactly_one_handoff_run(queue_env, monkeypatch):
    queue, sessions, _clock, _run = queue_env
    item = _accept(queue, "follow", delivery_mode="follow_up", replace_latest=False)
    journal = RunJournal(sessions)
    assert journal.claim("run-1", owner="worker-1", lease_seconds=90)
    handoff = {}

    def commit_effect(db):
        handoff.update(
            _commit_queued_handoff_in_session(
                db,
                source_run_id="run-1",
                chat_id="chat-1",
                user_id="user-1",
                session_messages=[{"role": "user", "content": "original"}],
                assistant_content="answer",
                context={"chat_id": "chat-1", "user_id": "user-1"},
                model_name="test-model",
            )
            or {}
        )

    assert journal.complete(
        "run-1",
        owner="worker-1",
        status="completed",
        commit_effect=commit_effect,
    )
    # A replaying/late worker cannot execute the effect a second time.
    assert not journal.complete(
        "run-1",
        owner="worker-1",
        status="completed",
        commit_effect=commit_effect,
    )

    with sessions() as db:
        applied = db.get(ChatSteerQueueItem, item.queue_id)
        next_run = db.get(ChatRun, handoff["run_id"])
        queued_messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.extra_data["steer_queue_id"].as_string() == item.queue_id)
            .all()
        )
        assert applied.status == "applied"
        assert applied.applied_run_id == next_run.run_id
        assert next_run.status == "pending"
        assert next_run.recovery_snapshot["kind"] == "chat"
        assert next_run.recovery_snapshot["worker_args"]["raw_user_message"] == item.message
        assert len(queued_messages) == 1
        assert queued_messages[0].content == item.message
        assert handoff["user_message_id"] == queued_messages[0].message_id

    from core.services import chat_steer_service

    monkeypatch.setattr(chat_steer_service, "SessionLocal", sessions)
    payload = chat_steer_service.list_run_steers("run-1")[0]
    assert payload["applied_run_id"] == next_run.run_id
    assert payload["applied_run_message_id"] == next_run.message_id
    assert payload["applied_user_message_id"] == queued_messages[0].message_id
    assert payload["applied_run_status"] == "pending"
