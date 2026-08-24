"""Alembic coverage for durable compaction sequence watermarks."""

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


def test_sqlite_compaction_upgrade_backfills_sequences_and_reupgrades(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'compaction-migration.sqlite'}"
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
                CREATE TABLE chat_messages (
                    message_id VARCHAR(64) PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL REFERENCES chat_sessions(chat_id),
                    role VARCHAR(20) NOT NULL,
                    content TEXT NOT NULL,
                    extra_data JSON,
                    created_at TIMESTAMP
                )
                """
            )
        )
        db.execute(text("INSERT INTO chat_sessions VALUES ('c1', 'u1', 'test')"))
        # Identical timestamps prove the deterministic message_id tie-breaker.
        db.execute(
            text(
                """
                INSERT INTO chat_messages VALUES
                    ('m2', 'c1', 'assistant', 'answer', NULL, '2026-08-24 00:00:00'),
                    ('m1', 'c1', 'user', 'question', NULL, '2026-08-24 00:00:00'),
                    ('m3', 'c1', 'user', 'arrived during summary', NULL,
                     '2026-08-24 00:00:01'),
                    ('z-legacy-checkpoint', 'c1', 'system', 'legacy summary',
                     '{"kind":"compaction_checkpoint","replacement_history":[]}',
                     '2026-08-24 00:00:02')
                """
            )
        )
        db.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('steerq01')"))

    _alembic(repo_root, database_url, "upgrade", "head")
    inspector = inspect(engine)
    assert "chat_compaction_states" in inspector.get_table_names()
    assert "chat_seq" in {
        column["name"] for column in inspector.get_columns("chat_messages")
    }
    assert "next_message_seq" in {
        column["name"] for column in inspector.get_columns("chat_sessions")
    }
    with engine.connect() as db:
        assert db.execute(
            text("SELECT message_id, chat_seq FROM chat_messages ORDER BY chat_seq")
        ).all() == [
            ("m1", 1),
            ("m2", 2),
            ("m3", 3),
            ("z-legacy-checkpoint", 4),
        ]
        assert (
            db.execute(
                text("SELECT next_message_seq FROM chat_sessions WHERE chat_id = 'c1'")
            ).scalar_one()
            == 4
        )
        # Runtime initialization must decide whether a legacy checkpoint is
        # safe.  The migration intentionally does not fabricate a watermark
        # from its insertion position.
        assert (
            db.execute(text("SELECT COUNT(*) FROM chat_compaction_states")).scalar_one()
            == 0
        )

    _alembic(repo_root, database_url, "downgrade", "steerq01")
    assert "chat_compaction_states" not in inspect(engine).get_table_names()
    assert "chat_seq" not in {
        column["name"] for column in inspect(engine).get_columns("chat_messages")
    }

    _alembic(repo_root, database_url, "upgrade", "head")
    assert "chat_compaction_states" in inspect(engine).get_table_names()
    engine.dispose()
