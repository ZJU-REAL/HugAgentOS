"""Alembic upgrade/downgrade coverage for ToolEffectLedger."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def _alembic(repo_root: Path, database_url: str, *args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_sqlite_tool_effect_upgrade_downgrade_and_reupgrade(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'tool-effect-migration.sqlite'}"
    engine = create_engine(database_url)
    # Start at the immediately preceding revision. The repository's historical
    # initial migration contains PostgreSQL-only JSONB, so a focused SQLite
    # migration test builds only the two predecessor tables this revision
    # alters/references (the same strategy as test_run_journal_migration.py).
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
                    run_phase VARCHAR(40) NOT NULL DEFAULT 'accepted',
                    lease_owner VARCHAR(160),
                    lease_expires_at TIMESTAMP,
                    operation_seq INTEGER NOT NULL DEFAULT 0,
                    snapshot_version INTEGER NOT NULL DEFAULT 0,
                    recovery_snapshot JSON,
                    last_operation_safety VARCHAR(40) NOT NULL DEFAULT 'replayable',
                    failure_reason TEXT,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
                """))
        db.execute(text("CREATE TABLE tool_call_logs (id VARCHAR(64) PRIMARY KEY)"))
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('runjrnl01')"))

    assert "effect_id" not in {
        column["name"] for column in inspect(engine).get_columns("tool_call_logs")
    }

    _alembic(repo_root, database_url, "upgrade", "head")
    inspector = inspect(engine)
    assert {
        "tool_effect_ledger",
        "tool_effect_leases",
        "tool_effect_receipts",
    }.issubset(inspector.get_table_names())
    assert {
        "effect_id",
        "result_id",
        "operation_seq",
        "idempotency_key",
        "recovery_policy",
        "terminal_effect_id",
    }.issubset({column["name"] for column in inspector.get_columns("tool_effect_ledger")})
    assert "run_owner" in {column["name"] for column in inspector.get_columns("tool_effect_leases")}
    assert "effect_id" in {column["name"] for column in inspector.get_columns("tool_call_logs")}

    _alembic(repo_root, database_url, "downgrade", "runjrnl01")
    inspector = inspect(engine)
    assert "tool_effect_ledger" not in inspector.get_table_names()
    assert "tool_effect_leases" not in inspector.get_table_names()
    assert "tool_effect_receipts" not in inspector.get_table_names()
    assert "effect_id" not in {column["name"] for column in inspector.get_columns("tool_call_logs")}

    _alembic(repo_root, database_url, "upgrade", "head")
    inspector = inspect(engine)
    assert "tool_effect_ledger" in inspector.get_table_names()
    assert "tool_effect_receipts" in inspector.get_table_names()
    assert "effect_id" in {column["name"] for column in inspector.get_columns("tool_call_logs")}
    engine.dispose()
