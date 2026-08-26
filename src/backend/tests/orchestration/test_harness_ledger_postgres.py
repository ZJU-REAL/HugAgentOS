"""Opt-in PostgreSQL concurrency coverage for harness sequence allocators."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from core.db.models import ChatSession, UserShadow
from core.harness.events import Event
from core.harness.usage import UsageAttempt
from core.services.harness_ledger import DurableEventStore, HarnessUsageLedger
from core.services.run_journal import RunJournal
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.getenv("RUN_JOURNAL_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set RUN_JOURNAL_POSTGRES_URL to run PostgreSQL integration",
)


def test_postgres_allocates_unique_usage_and_event_sequences_under_concurrency():
    repo_root = Path(__file__).resolve().parents[4]
    migration_env = dict(os.environ)
    migration_env["DATABASE_URL"] = POSTGRES_URL
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=migration_env,
        check=True,
        capture_output=True,
        text=True,
    )

    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_id = f"pg-hook-user-{suffix}"
    chat_id = f"pg-hook-chat-{suffix}"
    run_id = f"pg-hook-run-{suffix}"
    with sessions() as db:
        db.add(UserShadow(user_id=user_id, username=user_id))
        db.add(ChatSession(chat_id=chat_id, user_id=user_id, title="hook ledger"))
        db.commit()
    RunJournal(sessions).accept(
        run_id=run_id,
        message_id=f"pg-hook-message-{suffix}",
        chat_id=chat_id,
        user_id=user_id,
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    worker_count = 8
    usage_barrier = Barrier(worker_count)

    def record_usage(index: int) -> int:
        usage_barrier.wait(timeout=10)
        row = HarnessUsageLedger(sessions).record_attempt(
            UsageAttempt(
                run_id=run_id,
                kind="model",
                operation_name=f"model-{index}",
                provider="postgres-test",
                model="model",
                status="success",
                latency_ms=1,
                metadata={"index": index},
            )
        )
        return int(row.attempt_seq or 0)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        usage_sequences = sorted(pool.map(record_usage, range(worker_count)))
    assert usage_sequences == list(range(1, worker_count + 1))

    event_barrier = Barrier(worker_count)

    def record_event(index: int) -> int:
        event_barrier.wait(timeout=10)
        row = DurableEventStore(sessions)(
            Event(
                run_id=run_id,
                event_type="postgres_concurrency",
                phase="test",
                payload={"index": index},
                created_at=datetime.now(UTC),
            )
        )
        return int(row.event_seq or 0)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        event_sequences = sorted(pool.map(record_event, range(worker_count)))
    assert event_sequences == list(range(1, worker_count + 1))

    inspector = inspect(engine)
    usage_types = {
        column["name"]: column["type"]
        for column in inspector.get_columns("harness_usage_attempts")
    }
    event_types = {
        column["name"]: column["type"]
        for column in inspector.get_columns("harness_event_log")
    }
    assert isinstance(usage_types["attempt_metadata"], JSONB)
    assert isinstance(event_types["payload"], JSONB)
    engine.dispose()
