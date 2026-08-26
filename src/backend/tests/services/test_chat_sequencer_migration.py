"""Upgrade/downgrade proof for the chat sequencer migration."""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "harness65seq_chat_sequencer.py"
    )
    spec = importlib.util.spec_from_file_location("harness65seq_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(connection):
    metadata = MetaData()
    Table(
        "chat_sessions",
        metadata,
        Column("chat_id", String(64), primary_key=True),
        Column("user_id", String(64), nullable=False),
        Column("message_count", Integer, default=0),
        Column("updated_at", DateTime),
        Column("last_message_at", DateTime),
    )
    Table(
        "chat_messages",
        metadata,
        Column("message_id", String(64), primary_key=True),
        Column("chat_id", String(64), nullable=False),
        Column("role", String(20), nullable=False),
        Column("content", Text, nullable=False),
        Column("created_at", DateTime),
    )
    Table(
        "chat_runs",
        metadata,
        Column("run_id", String(64), primary_key=True),
        Column("chat_id", String(64), nullable=False),
        Column("user_id", String(64), nullable=False),
        Column("message_id", String(64), nullable=False),
        Column("status", String(20), nullable=False),
    )
    metadata.create_all(connection)
    return metadata


def _run_migration(connection, callback):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        callback()


def test_upgrade_backfills_stable_order_and_round_trips(tmp_path):
    migration = _load_migration_module()
    sqlite_engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with sqlite_engine.begin() as connection:
        metadata = _create_legacy_schema(connection)
        same = datetime(2026, 1, 1, 12, 0, 0)
        connection.execute(
            metadata.tables["chat_sessions"].insert(),
            [
                {"chat_id": "c1", "user_id": "u1", "message_count": 3},
                {"chat_id": "c2", "user_id": "u2", "message_count": 1},
            ],
        )
        connection.execute(
            metadata.tables["chat_messages"].insert(),
            [
                {
                    "message_id": "m2",
                    "chat_id": "c1",
                    "role": "assistant",
                    "content": "2",
                    "created_at": same,
                },
                {
                    "message_id": "m1",
                    "chat_id": "c1",
                    "role": "user",
                    "content": "1",
                    "created_at": same,
                },
                {
                    "message_id": "m3",
                    "chat_id": "c1",
                    "role": "user",
                    "content": "3",
                    "created_at": same,
                },
                {
                    "message_id": "x1",
                    "chat_id": "c2",
                    "role": "user",
                    "content": "x",
                    "created_at": same,
                },
            ],
        )
        _run_migration(connection, migration.upgrade)

        rows = connection.exec_driver_sql(
            "SELECT chat_id, message_id, chat_seq FROM chat_messages ORDER BY chat_id, chat_seq"
        ).all()
        assert rows == [
            ("c1", "m1", 1),
            ("c1", "m2", 2),
            ("c1", "m3", 3),
            ("c2", "x1", 1),
        ]
        assert connection.exec_driver_sql(
            "SELECT chat_id, next_message_seq FROM chat_sessions ORDER BY chat_id"
        ).all() == [("c1", 4), ("c2", 2)]

        inspector = inspect(connection)
        assert "uq_chat_messages_chat_seq" in {
            item["name"] for item in inspector.get_unique_constraints("chat_messages")
        }
        assert "uq_chat_runs_writer_slot" in {
            item["name"] for item in inspector.get_unique_constraints("chat_runs")
        }

        _run_migration(connection, migration.downgrade)
        assert "chat_seq" not in {
            item["name"] for item in inspect(connection).get_columns("chat_messages")
        }
        _run_migration(connection, migration.upgrade)
        assert "chat_seq" in {
            item["name"] for item in inspect(connection).get_columns("chat_messages")
        }


@pytest.mark.postgres
def test_postgresql_upgrade_downgrade_and_unique_constraints():
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is required for the PostgreSQL migration profile")

    migration = _load_migration_module()
    engine = create_engine(database_url)
    schema = f"harness65_{uuid.uuid4().hex[:12]}"
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        metadata = _create_legacy_schema(connection)
        same = datetime(2026, 1, 1, 12, 0, 0)
        connection.execute(
            metadata.tables["chat_sessions"].insert(),
            {"chat_id": "c1", "user_id": "u1", "message_count": 2},
        )
        connection.execute(
            metadata.tables["chat_messages"].insert(),
            [
                {
                    "message_id": "m2",
                    "chat_id": "c1",
                    "role": "assistant",
                    "content": "2",
                    "created_at": same,
                },
                {
                    "message_id": "m1",
                    "chat_id": "c1",
                    "role": "user",
                    "content": "1",
                    "created_at": same,
                },
            ],
        )
        _run_migration(connection, migration.upgrade)

        assert connection.execute(
            text("SELECT message_id, chat_seq FROM chat_messages ORDER BY chat_seq")
        ).all() == [("m1", 1), ("m2", 2)]

        duplicate_message = connection.begin_nested()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO chat_messages "
                    "(message_id, chat_id, role, content, created_at, chat_seq) "
                    "VALUES ('m3', 'c1', 'user', 'duplicate', :created_at, 1)"
                ),
                {"created_at": same},
            )
        duplicate_message.rollback()

        connection.execute(
            text(
                "INSERT INTO chat_runs "
                "(run_id, chat_id, user_id, message_id, status, writer_slot) "
                "VALUES ('r1', 'c1', 'u1', 'a1', 'pending', 'main')"
            )
        )
        duplicate_writer = connection.begin_nested()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO chat_runs "
                    "(run_id, chat_id, user_id, message_id, status, writer_slot) "
                    "VALUES ('r2', 'c1', 'u1', 'a2', 'pending', 'main')"
                )
            )
        duplicate_writer.rollback()

        _run_migration(connection, migration.downgrade)
        assert "chat_seq" not in {
            item["name"] for item in inspect(connection).get_columns("chat_messages")
        }
        _run_migration(connection, migration.upgrade)
        assert "chat_seq" in {
            item["name"] for item in inspect(connection).get_columns("chat_messages")
        }
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
