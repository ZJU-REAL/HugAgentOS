"""Public-seam tests for per-chat sequencing and main-run admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.db.models.chat import reserve_chat_sequences
from core.services.chat_sequencer import ChatBusyError, ChatSequencer
from core.services.chat_service import ChatService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _create_chat(db_session, chat_id: str = "chat-1", user_id: str = "user-1") -> ChatSession:
    session = ChatSession(chat_id=chat_id, user_id=user_id, title="test")
    db_session.add(session)
    db_session.commit()
    return session


def test_accept_main_run_commits_user_message_and_reserved_reply_sequence_together(db_session):
    _create_chat(db_session)

    accepted = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="hello",
        model="test-model",
        user_extra_data={"source": "test"},
        request_payload={"kind": "chat"},
    )

    stored_message = db_session.get(ChatMessage, accepted.user_message.message_id)
    stored_run = db_session.get(ChatRun, accepted.run.run_id)
    stored_chat = db_session.get(ChatSession, "chat-1")

    assert stored_message.chat_seq == 1
    assert stored_run.user_chat_seq == 1
    assert stored_run.assistant_chat_seq == 2
    assert stored_run.user_message_id == stored_message.message_id
    assert stored_run.writer_slot == "main"
    assert stored_chat.next_message_seq == 3
    assert stored_chat.message_count == 1


def test_busy_chat_rejects_second_run_without_orphan_message_or_sequence_gap(db_session):
    _create_chat(db_session)
    first = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )

    with pytest.raises(ChatBusyError) as raised:
        ChatSequencer(db_session).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="must-not-persist",
            request_payload={"kind": "chat"},
        )

    db_session.expire_all()
    assert raised.value.active_run.run_id == first.run.run_id
    assert db_session.query(ChatMessage).filter_by(chat_id="chat-1").count() == 1
    assert db_session.get(ChatSession, "chat-1").next_message_seq == 3


def test_terminal_transition_releases_writer_slot_for_next_run(db_session):
    _create_chat(db_session)
    sequencer = ChatSequencer(db_session)
    first = sequencer.accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )

    assert sequencer.release_writer(first.run.run_id, terminal_status="completed") is True

    second = sequencer.accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="second",
        request_payload={"kind": "chat"},
    )
    assert second.user_message.chat_seq == 3
    assert second.run.assistant_chat_seq == 4


async def test_restart_recovery_releases_writer_slot(db_session, monkeypatch):
    _create_chat(db_session)
    first = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )

    from orchestration import chat_run_executor

    Session = sessionmaker(bind=db_session.get_bind())

    async def fake_terminal(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_run_executor, "SessionLocal", Session)
    monkeypatch.setattr(chat_run_executor, "_write_terminal_to_stream", fake_terminal)

    assert await chat_run_executor.recover_orphan_runs() == 1
    db_session.expire_all()
    recovered = db_session.get(ChatRun, first.run.run_id)
    assert recovered.status == "failed"
    assert recovered.writer_slot is None

    second = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="after restart",
        request_payload={"kind": "chat"},
    )
    assert second.user_message.chat_seq == 3


async def test_cancel_keeps_writer_fenced_until_registered_worker_stops(db_session, monkeypatch):
    _create_chat(db_session)
    accepted = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )
    from orchestration import chat_run_executor

    Session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(chat_run_executor, "SessionLocal", Session)
    cleaning_up = asyncio.Event()

    async def worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning_up.set()
            await asyncio.sleep(0.05)
            raise

    task = asyncio.create_task(worker())
    chat_run_executor._active_runs[accepted.run.run_id] = task
    task.add_done_callback(
        lambda _task: chat_run_executor._active_runs.pop(accepted.run.run_id, None)
    )
    cancel_task = asyncio.create_task(
        chat_run_executor.cancel_run(accepted.run.run_id, user_id="user-1")
    )
    await cleaning_up.wait()

    with Session() as competing_db:
        with pytest.raises(ChatBusyError):
            ChatSequencer(competing_db).accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="must wait for cleanup",
                request_payload={"kind": "chat"},
            )

    assert await cancel_task is True
    with Session() as check_db:
        cancelled = check_db.get(ChatRun, accepted.run.run_id)
        assert (cancelled.status, cancelled.writer_slot) == ("cancelled", None)


async def test_cross_process_cancel_retains_writer_fence_until_worker_acknowledges(
    db_session, monkeypatch
):
    _create_chat(db_session)
    accepted = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )
    from orchestration import chat_run_executor

    Session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(chat_run_executor, "SessionLocal", Session)
    chat_run_executor._active_runs.pop(accepted.run.run_id, None)

    async def fake_terminal(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_run_executor, "_write_terminal_to_stream", fake_terminal)

    assert await chat_run_executor.cancel_run(accepted.run.run_id, user_id="user-1") is True
    with Session() as check_db:
        cancelled = check_db.get(ChatRun, accepted.run.run_id)
        assert (cancelled.status, cancelled.writer_slot) == ("cancelled", "main")
        with pytest.raises(ChatBusyError) as raised:
            ChatSequencer(check_db).accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="must stay fenced",
                request_payload={"kind": "chat"},
            )
        assert raised.value.active_run.status == "cancelled"


async def test_cancelled_pending_run_cannot_be_revived_by_late_remote_worker(
    db_session, monkeypatch
):
    _create_chat(db_session)
    accepted = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )
    from orchestration import chat_run_executor

    Session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(chat_run_executor, "SessionLocal", Session)
    chat_run_executor._active_runs.pop(accepted.run.run_id, None)

    async def fake_terminal(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_run_executor, "_write_terminal_to_stream", fake_terminal)
    assert await chat_run_executor.cancel_run(accepted.run.run_id, user_id="user-1") is True

    workflow_started = False

    async def forbidden_workflow(**_kwargs):
        nonlocal workflow_started
        workflow_started = True
        if False:  # pragma: no cover
            yield None

    async def fake_xadd(*_args, **_kwargs):
        return None

    async def fake_expire(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_run_executor, "astream_chat_workflow", forbidden_workflow)
    monkeypatch.setattr(chat_run_executor, "_xadd_event", fake_xadd)
    monkeypatch.setattr(chat_run_executor, "_expire_stream", fake_expire)

    await chat_run_executor._run_workflow(
        run_id=accepted.run.run_id,
        chat_id="chat-1",
        user_id="user-1",
        message_id=accepted.run.message_id,
        assistant_chat_seq=accepted.run.assistant_chat_seq,
        session_messages=[],
        effective_user_message="first",
        raw_user_message="first",
        context={},
        model_name="test-model",
    )

    assert workflow_started is False
    with Session() as check_db:
        cancelled = check_db.get(ChatRun, accepted.run.run_id)
        assert (cancelled.status, cancelled.writer_slot) == ("cancelled", None)


async def test_losing_worker_cannot_release_concurrently_cancelled_winner_fence(
    db_session, monkeypatch
):
    _create_chat(db_session)
    accepted = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )
    from orchestration import chat_run_executor

    Session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(chat_run_executor, "SessionLocal", Session)
    assert chat_run_executor._claim_run_execution(accepted.run.run_id) is True

    original_claim = chat_run_executor._claim_run_execution

    def lose_claim_then_cancel(run_id: str) -> bool:
        assert original_claim(run_id) is False
        assert chat_run_executor._request_run_cancel(run_id) is True
        return False

    workflow_started = False

    async def forbidden_workflow(**_kwargs):
        nonlocal workflow_started
        workflow_started = True
        if False:  # pragma: no cover
            yield None

    monkeypatch.setattr(chat_run_executor, "_claim_run_execution", lose_claim_then_cancel)
    monkeypatch.setattr(chat_run_executor, "astream_chat_workflow", forbidden_workflow)

    await chat_run_executor._run_workflow(
        run_id=accepted.run.run_id,
        chat_id="chat-1",
        user_id="user-1",
        message_id=accepted.run.message_id,
        assistant_chat_seq=accepted.run.assistant_chat_seq,
        session_messages=[],
        effective_user_message="first",
        raw_user_message="first",
        context={},
        model_name="test-model",
    )

    assert workflow_started is False
    with Session() as check_db:
        cancelled = check_db.get(ChatRun, accepted.run.run_id)
        assert cancelled.started_at is not None
        assert (cancelled.status, cancelled.writer_slot) == ("cancelled", "main")
        with pytest.raises(ChatBusyError):
            ChatSequencer(check_db).accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="must remain fenced",
                request_payload={"kind": "chat"},
            )


def test_synchronous_launch_failure_releases_writer_slot(db_session):
    _create_chat(db_session)
    sequencer = ChatSequencer(db_session)
    accepted = sequencer.accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="first",
        request_payload={"kind": "chat"},
    )

    assert sequencer.abandon_pending_run(accepted.run.run_id, reason="launch exploded") is True
    db_session.expire_all()
    failed = db_session.get(ChatRun, accepted.run.run_id)
    assert (failed.status, failed.writer_slot, failed.error_message) == (
        "failed",
        None,
        "launch exploded",
    )

    second = sequencer.accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="retry",
        request_payload={"kind": "chat"},
    )
    assert second.user_message.chat_seq == 3


def test_existing_user_turn_can_reserve_a_new_reply_without_duplicating_user_message(db_session):
    _create_chat(db_session)
    user_message = ChatService(db_session).add_message(
        chat_id="chat-1", role="user", content="retry me"
    )

    accepted = ChatSequencer(db_session).accept_existing_user_run(
        chat_id="chat-1",
        user_id="user-1",
        user_message_id=user_message.message_id,
        request_payload={"kind": "batch_resume"},
    )

    assert accepted.run.user_chat_seq == 1
    assert accepted.run.assistant_chat_seq == 2
    assert db_session.query(ChatMessage).filter_by(chat_id="chat-1").count() == 1


def test_busy_rewrite_rolls_back_history_deletion_and_sequence_reservation(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    original_user = service.add_message(chat_id="chat-1", role="user", content="question")
    original_reply = service.add_message(chat_id="chat-1", role="assistant", content="answer")
    active = ChatSequencer(db_session).accept_main_run(
        chat_id="chat-1",
        user_id="user-1",
        user_content="currently running",
        request_payload={"kind": "chat"},
    )
    next_before = db_session.get(ChatSession, "chat-1").next_message_seq

    with pytest.raises(ChatBusyError) as raised:
        ChatSequencer(db_session).accept_existing_user_run(
            chat_id="chat-1",
            user_id="user-1",
            user_message_id=original_user.message_id,
            delete_from_message_id=original_reply.message_id,
            delete_from_chat_seq=original_reply.chat_seq,
            request_payload={"kind": "regenerate"},
        )

    db_session.expire_all()
    assert raised.value.active_run.run_id == active.run.run_id
    assert [
        (row.chat_seq, row.content)
        for row in db_session.query(ChatMessage).order_by(ChatMessage.chat_seq)
    ] == [(1, "question"), (2, "answer"), (3, "currently running")]
    assert db_session.get(ChatSession, "chat-1").next_message_seq == next_before


def test_existing_user_rewrite_deletes_tail_in_the_admission_transaction(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    original_user = service.add_message(chat_id="chat-1", role="user", content="question")
    original_reply = service.add_message(chat_id="chat-1", role="assistant", content="answer")
    service.add_message(chat_id="chat-1", role="user", content="later")

    accepted = ChatSequencer(db_session).accept_existing_user_run(
        chat_id="chat-1",
        user_id="user-1",
        user_message_id=original_user.message_id,
        delete_from_message_id=original_reply.message_id,
        delete_from_chat_seq=original_reply.chat_seq,
        request_payload={"kind": "regenerate"},
    )

    assert [(row.chat_seq, row.content) for row in db_session.query(ChatMessage).all()] == [
        (1, "question")
    ]
    assert accepted.run.user_chat_seq == 1
    assert accepted.run.assistant_chat_seq == 4
    assert db_session.get(ChatSession, "chat-1").message_count == 1


def test_rewrite_rejects_a_stale_delete_anchor_without_deleting_newer_history(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    original_user = service.add_message(chat_id="chat-1", role="user", content="question")
    original_reply = service.add_message(chat_id="chat-1", role="assistant", content="answer")
    service.add_message(chat_id="chat-1", role="user", content="later")
    next_before = db_session.get(ChatSession, "chat-1").next_message_seq

    with pytest.raises(ValueError, match="rewrite boundary"):
        ChatSequencer(db_session).accept_existing_user_run(
            chat_id="chat-1",
            user_id="user-1",
            user_message_id=original_user.message_id,
            delete_from_message_id="missing-message",
            delete_from_chat_seq=original_reply.chat_seq,
            request_payload={"kind": "regenerate"},
        )

    db_session.expire_all()
    assert [
        row.content for row in db_session.query(ChatMessage).order_by(ChatMessage.chat_seq)
    ] == [
        "question",
        "answer",
        "later",
    ]
    assert db_session.get(ChatSession, "chat-1").next_message_seq == next_before


def test_replacement_user_and_tail_deletion_are_one_admission_transaction(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    service.add_message(chat_id="chat-1", role="user", content="keep")
    target = service.add_message(chat_id="chat-1", role="user", content="old wording")
    service.add_message(chat_id="chat-1", role="assistant", content="old reply")

    accepted = ChatSequencer(db_session).accept_replacement_user_run(
        chat_id="chat-1",
        user_id="user-1",
        target_user_message_id=target.message_id,
        delete_from_chat_seq=target.chat_seq,
        user_content="new wording",
        request_payload={"kind": "edit"},
        user_extra_data={"edited": True},
    )

    assert [
        (row.chat_seq, row.content)
        for row in db_session.query(ChatMessage).order_by(ChatMessage.chat_seq)
    ] == [(1, "keep"), (4, "new wording")]
    assert accepted.user_message.chat_seq == accepted.run.user_chat_seq == 4
    assert accepted.run.assistant_chat_seq == 5
    assert db_session.get(ChatSession, "chat-1").message_count == 2


def test_message_order_uses_chat_seq_when_timestamps_are_identical(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    first = service.add_message(chat_id="chat-1", role="user", content="first")
    second = service.add_message(chat_id="chat-1", role="assistant", content="second")

    same_time = datetime(2026, 1, 1, 12, 0, 0)
    db_session.query(ChatMessage).filter(
        ChatMessage.message_id.in_([first.message_id, second.message_id])
    ).update({"created_at": same_time}, synchronize_session=False)
    db_session.commit()

    rows, total = service.message_repo.list_by_chat("chat-1", page=1, page_size=10)
    assert total == 2
    assert [(row.chat_seq, row.content) for row in rows] == [(1, "first"), (2, "second")]


def test_delete_from_uses_sequence_boundary_not_timestamp(db_session):
    _create_chat(db_session)
    service = ChatService(db_session)
    first = service.add_message(chat_id="chat-1", role="user", content="first")
    second = service.add_message(chat_id="chat-1", role="assistant", content="second")
    third = service.add_message(chat_id="chat-1", role="user", content="third")
    first_id, second_id, third_id = first.message_id, second.message_id, third.message_id

    same_time = datetime(2026, 1, 1, 12, 0, 0)
    db_session.query(ChatMessage).filter(ChatMessage.chat_id == "chat-1").update(
        {"created_at": same_time}, synchronize_session=False
    )
    db_session.commit()

    assert service.delete_messages_from("chat-1", second_id) == 2
    assert [row.message_id for row in db_session.query(ChatMessage).all()] == [first_id]
    assert third_id != first_id


def _concurrent_database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sequencer.db'}",
        connect_args={"check_same_thread": False, "timeout": 60},
    )
    Base.metadata.create_all(
        engine,
        tables=[ChatSession.__table__, ChatMessage.__table__, ChatRun.__table__],
    )
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    return engine, Session


def test_different_chats_accept_concurrently_without_sharing_a_writer_lock(tmp_path):
    engine, Session = _concurrent_database(tmp_path)
    with Session() as db:
        db.add(ChatSession(chat_id="chat-2", user_id="user-1", title="test 2"))
        db.commit()
    barrier = Barrier(2)

    def accept(chat_id):
        barrier.wait()
        with Session() as db:
            accepted = ChatSequencer(db).accept_main_run(
                chat_id=chat_id,
                user_id="user-1",
                user_content=f"hello from {chat_id}",
                request_payload={"chat_id": chat_id},
            )
            return (
                accepted.run.chat_id,
                accepted.run.writer_slot,
                accepted.user_message.chat_seq,
                accepted.run.assistant_chat_seq,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, ("chat-1", "chat-2")))

    assert sorted(outcomes) == [
        ("chat-1", "main", 1, 2),
        ("chat-2", "main", 1, 2),
    ]
    with Session() as db:
        assert db.query(ChatRun).count() == 2
        assert db.query(ChatMessage).count() == 2
    engine.dispose()


def test_fifty_concurrent_allocations_are_unique_and_gap_free(tmp_path):
    engine, Session = _concurrent_database(tmp_path)
    barrier = Barrier(50)

    def allocate_one(_index):
        barrier.wait()
        with Session() as db:
            seq = reserve_chat_sequences(db, "chat-1")
            db.commit()
            return seq

    with ThreadPoolExecutor(max_workers=50) as pool:
        allocated = list(pool.map(allocate_one, range(50)))

    assert sorted(allocated) == list(range(1, 51))
    with Session() as db:
        assert db.get(ChatSession, "chat-1").next_message_seq == 51
    engine.dispose()


def test_two_concurrent_admissions_have_exactly_one_winner(tmp_path):
    engine, Session = _concurrent_database(tmp_path)
    barrier = Barrier(2)

    def accept(label):
        barrier.wait()
        with Session() as db:
            try:
                accepted = ChatSequencer(db).accept_main_run(
                    chat_id="chat-1",
                    user_id="user-1",
                    user_content=label,
                    request_payload={"label": label},
                )
                return "accepted", accepted.run.run_id
            except ChatBusyError as exc:
                return "busy", exc.active_run.run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, ("first", "second")))

    assert sorted(kind for kind, _run_id in outcomes) == ["accepted", "busy"]
    assert outcomes[0][1] == outcomes[1][1]
    with Session() as db:
        assert db.query(ChatMessage).count() == 1
        assert db.query(ChatRun).count() == 1
        assert db.get(ChatSession, "chat-1").next_message_seq == 3
    engine.dispose()
