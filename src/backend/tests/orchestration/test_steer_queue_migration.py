"""Alembic upgrade/downgrade coverage for the durable steer queue."""

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


def test_sqlite_steer_queue_upgrade_downgrade_and_reupgrade(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'steer-queue-migration.sqlite'}"
    engine = create_engine(database_url)
    with engine.begin() as db:
        db.execute(
            text(
                """
                CREATE TABLE chat_sessions (
                    chat_id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    title VARCHAR(500) NOT NULL
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TABLE chat_runs (
                    run_id VARCHAR(64) PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL REFERENCES chat_sessions(chat_id),
                    user_id VARCHAR(64) NOT NULL,
                    message_id VARCHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL
                )
                """
            )
        )
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('tooleff01')"))

    _alembic(repo_root, database_url, "upgrade", "head")
    inspector = inspect(engine)
    assert "chat_steer_queue" in inspector.get_table_names()
    assert {
        "queue_id",
        "steer_id",
        "steer_seq",
        "target_run_id",
        "target_operation_seq",
        "delivery_mode",
        "status",
        "lease_owner",
        "lease_expires_at",
        "applied_run_id",
        "applied_source_run_id",
        "applied_operation_seq",
    }.issubset({column["name"] for column in inspector.get_columns("chat_steer_queue")})

    _alembic(repo_root, database_url, "downgrade", "tooleff01")
    assert "chat_steer_queue" not in inspect(engine).get_table_names()

    _alembic(repo_root, database_url, "upgrade", "head")
    assert "chat_steer_queue" in inspect(engine).get_table_names()
    engine.dispose()
