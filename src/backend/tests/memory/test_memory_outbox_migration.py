"""Upgrade/downgrade proof for MemoryOutbox and ProfileMemory revision."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.db.models import MemoryOutbox
from core.memory import effect_lane as E


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "harness69mem_memory_outbox_profile_cas.py"
    )
    spec = importlib.util.spec_from_file_location("harness69mem_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(connection):
    metadata = MetaData()
    Table(
        "profile_memory",
        metadata,
        Column("user_id", String(64), primary_key=True),
        Column("workspace_id", String(64), primary_key=True),
        Column("content_md", Text, nullable=False),
    )
    metadata.create_all(connection)
    return metadata


def _run_migration(connection, callback):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        callback()


def _exercise(connection):
    migration = _load_migration_module()
    metadata = _create_legacy_schema(connection)
    connection.execute(
        metadata.tables["profile_memory"].insert(),
        {"user_id": "u1", "workspace_id": "default", "content_md": "legacy"},
    )
    _run_migration(connection, migration.upgrade)

    assert connection.execute(
        text(
            "SELECT content_md, revision FROM profile_memory "
            "WHERE user_id='u1' AND workspace_id='default'"
        )
    ).one() == ("legacy", 0)
    assert "effect_receipts" in {
        item["name"] for item in inspect(connection).get_columns("profile_memory")
    }
    assert "memory_outbox" in inspect(connection).get_table_names()
    assert "uq_memory_outbox_candidate" in {
        item["name"] for item in inspect(connection).get_unique_constraints("memory_outbox")
    }
    assert "idx_memory_outbox_scope_lane" in {
        item["name"] for item in inspect(connection).get_indexes("memory_outbox")
    }

    outbox = Table("memory_outbox", MetaData(), autoload_with=connection)
    connection.execute(
        outbox.insert(),
        {
            "id": "j1",
            "message_id": "m1",
            "job_kind": "candidate",
            "layer": "L1:identity",
            "candidate_hash": "h1",
            "payload_json": {},
        },
    )
    duplicate = connection.begin_nested()
    with pytest.raises(IntegrityError):
        connection.execute(
            outbox.insert(),
            {
                "id": "j2",
                "message_id": "m1",
                "job_kind": "candidate",
                "layer": "L1:identity",
                "candidate_hash": "h1",
                "payload_json": {},
            },
        )
    duplicate.rollback()

    _run_migration(connection, migration.downgrade)
    assert "revision" not in {
        item["name"] for item in inspect(connection).get_columns("profile_memory")
    }
    assert "effect_receipts" not in {
        item["name"] for item in inspect(connection).get_columns("profile_memory")
    }
    _run_migration(connection, migration.upgrade)
    assert "revision" in {
        item["name"] for item in inspect(connection).get_columns("profile_memory")
    }


def test_sqlite_upgrade_downgrade_and_idempotency_constraint(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory-migration.db'}")
    with engine.begin() as connection:
        _exercise(connection)


@pytest.mark.postgres
def test_postgresql_upgrade_downgrade_and_idempotency_constraint():
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is required for the PostgreSQL migration profile")

    engine = create_engine(database_url)
    schema = f"harness69_{uuid.uuid4().hex[:12]}"
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _exercise(connection)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgresql_effect_lane_serializes_connections_and_checks_oldest(monkeypatch):
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is required for the PostgreSQL lane profile")

    migration = _load_migration_module()
    admin_engine = create_engine(database_url)
    schema = f"harness69_lane_{uuid.uuid4().hex[:12]}"
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _create_legacy_schema(connection)
        _run_migration(connection, migration.upgrade)

    lane_engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=4,
        max_overflow=0,
    )
    factory = sessionmaker(bind=lane_engine)
    monkeypatch.setattr(E, "SessionLocal", factory)
    scope_key = "postgres-lane-scope"
    try:
        with factory() as db:
            db.add(
                MemoryOutbox(
                    id="job-a",
                    message_id="m-a",
                    scope_key=scope_key,
                    job_kind="candidate",
                    layer="L2:procedural",
                    candidate_hash="hash-a",
                    payload_json={},
                    status="processing",
                    lease_owner="worker-a",
                )
            )
            db.commit()

        entered: list[str] = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_lane(label: str):
            async with E.ordered_l2_effect(scope_key, "job-a"):
                entered.append(label)
                if label == "first":
                    first_entered.set()
                    await release_first.wait()

        first = asyncio.create_task(hold_lane("first"))
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        second = asyncio.create_task(hold_lane("second"))
        await asyncio.sleep(0.15)
        assert entered == ["first"]
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
        assert entered == ["first", "second"]

        with factory() as db:
            db.add(
                MemoryOutbox(
                    id="job-b",
                    message_id="m-b",
                    scope_key=scope_key,
                    job_kind="candidate",
                    layer="L2:procedural",
                    candidate_hash="hash-b",
                    payload_json={},
                    status="processing",
                    lease_owner="worker-b",
                )
            )
            db.commit()

        with pytest.raises(E.EffectLaneDeferred):
            async with E.ordered_l2_effect(scope_key, "job-b"):
                pytest.fail("a later PostgreSQL effect must not overtake the oldest row")
    finally:
        lane_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
