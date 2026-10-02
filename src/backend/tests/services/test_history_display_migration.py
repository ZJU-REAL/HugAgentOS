import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_preview_schema_upgrade_and_rollback_preserve_original(tmp_path):
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/historydisplay01_add_tool_display.py"
    )
    spec = importlib.util.spec_from_file_location("history_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path/'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE chat_messages (message_id TEXT PRIMARY KEY, tool_calls JSON)")
        )
        connection.execute(
            text("INSERT INTO chat_messages VALUES (:id,:tools)"),
            {"id": "m", "tools": '[{"result":"original"}]'},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "tool_calls_display" in {
                c["name"] for c in inspect(connection).get_columns("chat_messages")
            }
            assert (
                connection.execute(text("SELECT tool_calls_display FROM chat_messages")).scalar()
                is None
            )
            migration.downgrade()
        assert (
            connection.execute(text("SELECT tool_calls FROM chat_messages")).scalar()
            == '[{"result":"original"}]'
        )
