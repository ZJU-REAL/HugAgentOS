"""Optional tests against the disposable perf172 PostgreSQL fixture only."""

import os
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.db.models import ChatSession, ChatMessage
from core.db.repository.chat import ChatSessionRepository
from core.db.history_page import history_page


@pytest.fixture
def db():
    url = os.getenv("PERF_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("requires disposable perf172 PostgreSQL")
    assert "127.0.0.1:55472/perf" in url
    engine = create_engine(url)
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text(
                "CREATE TEMP TABLE chat_sessions (LIKE public.chat_sessions INCLUDING ALL) ON COMMIT DROP"
            )
        )
        connection.execute(
            text(
                "CREATE TEMP TABLE chat_messages (LIKE public.chat_messages INCLUDING ALL) ON COMMIT DROP"
            )
        )
        with Session(bind=connection) as session:
            yield session
        transaction.rollback()
    engine.dispose()


def test_postgres_search_visible_text_and_literals(db):
    db.add(ChatSession(chat_id="c", user_id="u", title="ordinary"))
    db.add(ChatSession(chat_id="private", user_id="other", title="needle"))
    db.flush()
    for i in range(22):
        db.add(
            ChatMessage(
                message_id=f"m{i}",
                chat_id="c",
                chat_seq=i + 1,
                role="assistant",
                content=(
                    "needle</think>hidden"
                    if i < 21
                    else "  visible<think>secret</think> needle 100%_real  "
                ),
            )
        )
    db.flush()
    rows, total = ChatSessionRepository(db).search("u", "needle", scope="all")
    assert total == 1 and rows[0]["session"].chat_id == "c"
    assert "secret" not in rows[0]["matched_snippet"]
    assert ChatSessionRepository(db).search("u", "100%_real", scope="all")[1] == 1
    assert ChatSessionRepository(db).search("u", "100%Xreal", scope="all")[1] == 0


def test_postgres_history_display_and_bulk_updates(db):
    db.add(ChatSession(chat_id="c", user_id="u", title="x"))
    db.flush()
    db.add(
        ChatMessage(
            message_id="m",
            chat_id="c",
            chat_seq=1,
            role="assistant",
            content="x",
            tool_calls=[{"result": "x" * 100000}],
        )
    )
    db.flush()
    rows, _, _ = history_page(db, "c")
    assert rows[0].display_is_bounded
    assert len(rows[0].tool_calls[0]["result"]) < 3000
    assert len(db.get(ChatMessage, "m").tool_calls[0]["result"]) == 100000
