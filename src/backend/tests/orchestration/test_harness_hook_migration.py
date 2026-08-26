"""Alembic coverage for neutral event/usage ledgers."""

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


def test_sqlite_hook_ledgers_upgrade_downgrade_and_reupgrade(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'hook-ledger-migration.sqlite'}"
    engine = create_engine(database_url)
    with engine.begin() as db:
        db.execute(
            text(
                """
                CREATE TABLE chat_runs (
                    run_id VARCHAR(64) PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    message_id VARCHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL
                )
                """
            )
        )
        db.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        db.execute(
            text("INSERT INTO alembic_version(version_num) VALUES ('compact01')")
        )

    _alembic(repo_root, database_url, "upgrade", "head")
    assert {
        "harness_usage_cursors",
        "harness_usage_attempts",
        "harness_event_cursors",
        "harness_event_log",
    }.issubset(inspect(engine).get_table_names())
    assert {"attempt_seq", "retry_of", "effect_id", "cache_read_tokens"}.issubset(
        {
            column["name"]
            for column in inspect(engine).get_columns("harness_usage_attempts")
        }
    )

    _alembic(repo_root, database_url, "downgrade", "compact01")
    assert "harness_usage_attempts" not in inspect(engine).get_table_names()
    assert "harness_event_log" not in inspect(engine).get_table_names()

    _alembic(repo_root, database_url, "upgrade", "head")
    assert "harness_usage_attempts" in inspect(engine).get_table_names()
    engine.dispose()
