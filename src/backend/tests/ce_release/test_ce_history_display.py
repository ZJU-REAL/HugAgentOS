from pathlib import Path
from sqlalchemy import text, inspect as inspect_database


def test_ce_tool_calls_display_migration_preserves_existing_messages(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from core.db.edition_tables import ce_create_all
    from core.db.models import ChatMessage, ChatSession
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'ce-history-display.db'}")
    ce_create_all(engine)
    with Session(engine) as session:
        session.add(ChatSession(chat_id="old-chat", user_id="old-user", title="History"))
        session.add(ChatMessage(message_id="old-message", chat_id="old-chat",
                                role="assistant", content="Preserved answer"))
        session.commit()
    path = Path(__file__).resolve().parents[2] / "alembic/versions/ce_0019_history_display.py"
    spec = importlib.util.spec_from_file_location("ce_0019_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE chat_messages DROP COLUMN tool_calls_display'))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert "tool_calls_display" in {c["name"] for c in inspect_database(connection).get_columns("chat_messages")}
        row = connection.execute(text("SELECT content, tool_calls_display FROM chat_messages WHERE message_id='old-message'")).one()
        assert row[0] == "Preserved answer"
        assert row[1] is None
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.execute(text("SELECT content FROM chat_messages WHERE message_id='old-message'")).scalar() == "Preserved answer"
    engine.dispose()
