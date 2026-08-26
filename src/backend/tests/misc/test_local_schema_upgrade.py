from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from core.db.local_schema_upgrade import reconcile_local_chat_sequences


def _legacy_db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE chat_sessions ("
            "chat_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE chat_messages ("
            "message_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64) NOT NULL, "
            "role VARCHAR(20) NOT NULL, content TEXT NOT NULL, created_at TIMESTAMP)"
        )
        connection.execute(
            text("INSERT INTO chat_sessions (chat_id, user_id) VALUES (:chat, 'u')"),
            [{"chat": "a"}, {"chat": "b"}],
        )
        connection.execute(
            text(
                "INSERT INTO chat_messages "
                "(message_id, chat_id, role, content, created_at) "
                "VALUES (:message, :chat, 'user', :message, :created)"
            ),
            [
                {"message": "a2", "chat": "a", "created": "2026-01-02"},
                {"message": "a1", "chat": "a", "created": "2026-01-01"},
                {"message": "b1", "chat": "b", "created": "2026-01-01"},
            ],
        )
    return engine


def test_legacy_chat_sequences_are_backfilled_and_idempotent():
    engine = _legacy_db()

    first = reconcile_local_chat_sequences(engine)
    second = reconcile_local_chat_sequences(engine)

    assert first == {
        "columns": ["chat_messages.chat_seq", "chat_sessions.next_message_seq"],
        "backfills": ["chat_messages.chat_seq:3"],
        "indexes": ["uq_chat_messages_chat_seq"],
    }
    assert second == {"columns": [], "backfills": [], "indexes": []}
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT message_id, chat_seq FROM chat_messages "
                "ORDER BY chat_id, chat_seq"
            )
        ).all() == [("a1", 1), ("a2", 2), ("b1", 1)]
        assert connection.execute(
            text("SELECT chat_id, next_message_seq FROM chat_sessions ORDER BY chat_id")
        ).all() == [("a", 3), ("b", 2)]
        assert "uq_chat_messages_chat_seq" in {
            item["name"] for item in inspect(connection).get_indexes("chat_messages")
        }


def test_newer_startup_repairs_messages_written_by_a_rollback_target():
    engine = _legacy_db()
    reconcile_local_chat_sequences(engine)
    with engine.begin() as connection:
        # A pre-chat_seq release can still insert while rollback is active.
        connection.exec_driver_sql(
            "INSERT INTO chat_messages "
            "(message_id, chat_id, role, content, created_at) "
            "VALUES ('a3', 'a', 'assistant', 'legacy', '2026-01-03')"
        )

    report = reconcile_local_chat_sequences(engine)

    assert report["backfills"] == ["chat_messages.chat_seq:1"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT chat_seq FROM chat_messages WHERE message_id = 'a3'")
        ).scalar_one() == 3
        assert connection.execute(
            text("SELECT next_message_seq FROM chat_sessions WHERE chat_id = 'a'")
        ).scalar_one() == 4
