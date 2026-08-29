"""Fresh SQLite installation and Harness migration round-trip coverage."""

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


def _revision(engine) -> str:
    with engine.connect() as db:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _assert_harness_head(engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "chat_runs",
        "chat_run_operations",
        "tool_effect_ledger",
        "tool_effect_leases",
        "tool_effect_receipts",
        "chat_steer_queue",
        "chat_compaction_states",
        "harness_event_log",
        "harness_usage_attempts",
        "memory_outbox",
    }.issubset(tables)
    profile_columns = {
        column["name"] for column in inspector.get_columns("profile_memory")
    }
    assert {"revision", "effect_receipts"}.issubset(profile_columns)
    loop_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("agent_loops")
    }
    assert "interrupted" in loop_checks["agent_loops_status_check"]


def test_fresh_sqlite_full_chain_and_harness_roundtrip(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'fresh-harness.sqlite'}"
    engine = create_engine(database_url)

    _alembic(repo_root, database_url, "upgrade", "head")
    assert _revision(engine) == "think01split"
    _assert_harness_head(engine)

    _alembic(repo_root, database_url, "downgrade", "pluginui01")
    assert _revision(engine) == "pluginui01"
    downgraded_tables = set(inspect(engine).get_table_names())
    assert "chat_run_operations" not in downgraded_tables
    assert "memory_outbox" not in downgraded_tables

    _alembic(repo_root, database_url, "upgrade", "head")
    assert _revision(engine) == "think01split"
    _assert_harness_head(engine)
    engine.dispose()
