"""PostgreSQL regression: a late streaming save must not erase final usage."""

import threading
import time
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from core.chat import inflight
from core.db.models import ChatMessage, ChatSession, UserShadow
from core.infra.time import utc_now
from core.chat.sequencer import ChatSequencer
from core.chat.service import ChatService
from core.runs.journal import RunJournal


def verify_finalization_serializes_refresh(sessions, user_id):
    chat = "ctx-race-" + uuid.uuid4().hex[:12]
    with sessions() as db:
        db.add(ChatSession(chat_id=chat, user_id=user_id, title="合成并发验收"))
        db.commit()
        run = (
            ChatSequencer(db)
            .accept_main_run(
                chat_id=chat,
                user_id=user_id,
                user_content="synthetic concurrency test",
                request_payload={"message": "test"},
            )
            .run
        )
    journal = RunJournal(sessions)
    assert journal.claim(run.run_id, owner="race-worker", lease_seconds=60)
    final_locked = threading.Event()
    release_final = threading.Event()
    refresh_started = threading.Event()
    outcomes = []
    errors = []

    def effect(db):
        ChatService(db).upsert_message(
            chat,
            "assistant",
            "final",
            message_id=run.message_id,
            extra_data={"context_usage": {"used_tokens": 12345}},
            commit=False,
        )
        db.flush()
        final_locked.set()
        assert release_final.wait(5)

    def finalize():
        try:
            assert journal.complete(
                run.run_id, owner="race-worker", status="completed", commit_effect=effect
            )
        except BaseException as exc:
            errors.append(exc)

    def refresh():
        try:
            with sessions() as db:
                refresh_started.set()
                outcomes.append(
                    ChatService(db).refresh_streaming_message(
                        run_id=run.run_id,
                        owner="race-worker",
                        message_id=run.message_id,
                        content="stale streaming",
                        thinking=None,
                        tool_calls=None,
                        extra_data={inflight.IN_FLIGHT_KEY: inflight.mark(run.run_id, "streaming")},
                    )
                )
                db.commit()
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=finalize)
    b = threading.Thread(target=refresh)
    try:
        a.start()
        assert final_locked.wait(5)
        b.start()
        assert refresh_started.wait(5)
        time.sleep(0.2)  # Ensure the old MVCC snapshot reaches the competing lock.
        release_final.set()
        a.join(8)
        b.join(8)
        assert not a.is_alive() and not b.is_alive()
        assert not errors, errors
        assert outcomes == [False]
        with sessions() as db:
            message = db.get(ChatMessage, run.message_id)
            assert message.content == "final"
            assert message.extra_data["context_usage"]["used_tokens"] == 12345
            assert inflight.marker(message.extra_data) is None
    finally:
        release_final.set()
        with sessions() as db:
            db.get(ChatSession, chat).deleted_at = utc_now()
            db.commit()


def test_postgres_final_commit_fences_late_refresh(db_session):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL MVCC; also executed against the local container DB")
    db_session.add(UserShadow(user_id="race-test-user", username="synthetic-race-user"))
    db_session.commit()
    verify_finalization_serializes_refresh(
        sessionmaker(bind=db_session.get_bind()), "race-test-user"
    )
