"""Alembic upgrade/downgrade coverage for the SQLite run-journal schema."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from tests.db_support import run_alembic


def test_sqlite_legacy_upgrade_downgrade_and_reupgrade(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    db_path = tmp_path / "legacy-run-journal.sqlite"
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url)
    with engine.begin() as db:
        db.execute(text("""
                CREATE TABLE chat_runs (
                    run_id VARCHAR(64) PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    message_id VARCHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    request_payload JSON,
                    last_event_offset INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    usage JSON,
                    created_at TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    CONSTRAINT chat_runs_status_check CHECK (
                        status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
                    )
                )
                """))
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('pluginui01')"))
        db.execute(text("""
                INSERT INTO chat_runs(
                    run_id, chat_id, user_id, message_id, status,
                    request_payload, last_event_offset, created_at
                ) VALUES (
                    'legacy-run', 'legacy-chat', 'legacy-user', 'legacy-message',
                    'running', '{"message":"before journal"}', 7, CURRENT_TIMESTAMP
                )
                """))

    run_alembic(repo_root, database_url, "upgrade", "runjrnl01")
    inspector = inspect(engine)
    assert "chat_run_operations" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("chat_runs")}
    assert {"lease_owner", "operation_seq", "snapshot_version", "recovery_snapshot"}.issubset(
        columns
    )
    with engine.connect() as db:
        legacy = db.execute(
            text("SELECT run_phase, last_event_offset FROM chat_runs " "WHERE run_id='legacy-run'")
        ).one()
        assert legacy.run_phase == "legacy_unrecoverable"
        assert legacy.last_event_offset == 7

    run_alembic(repo_root, database_url, "downgrade", "pluginui01")
    inspector = inspect(engine)
    assert "chat_run_operations" not in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("chat_runs")}
    assert "lease_owner" not in columns
    assert "run_phase" not in columns

    run_alembic(repo_root, database_url, "upgrade", "runjrnl01")
    inspector = inspect(engine)
    assert "chat_run_operations" in inspector.get_table_names()
    with engine.connect() as db:
        assert (
            db.execute(
                text("SELECT run_phase FROM chat_runs WHERE run_id='legacy-run'")
            ).scalar_one()
            == "legacy_unrecoverable"
        )
    engine.dispose()
