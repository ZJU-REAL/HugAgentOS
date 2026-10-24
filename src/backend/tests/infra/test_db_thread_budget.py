"""The connection pool and the thread limits come from one budget, and DB work runs in it."""

import asyncio
import os
import threading

import anyio.to_thread
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db import engine, threads


def test_pool_is_one_connection_per_thread_that_can_use_it(monkeypatch):
    from dataclasses import replace
    from core.config.settings import settings

    original = settings.db
    object.__setattr__(settings, "db", replace(original, worker_threads=24))
    try:
        budget = engine.thread_budget()
    finally:
        object.__setattr__(settings, "db", original)
    assert budget.request_threads == 24
    assert budget.background_threads == min(32, (os.cpu_count() or 1) + 4)
    assert budget.pool_size == budget.request_threads + budget.background_threads
    assert budget.overflow == 10


def test_worker_threads_setting_reads_its_environment(monkeypatch):
    from core.config.identity_settings import DatabaseSettings

    monkeypatch.setenv("WORKER_THREADS", "12")
    assert DatabaseSettings().worker_threads == 12
    monkeypatch.setenv("WORKER_THREADS", "0")
    assert DatabaseSettings().worker_threads == 1


def test_postgres_engine_takes_its_pool_from_the_budget():
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "from core.db import engine as e; b = e.thread_budget(); "
        "assert e.engine.pool.size() == b.pool_size == 7 + b.background_threads, e.engine.pool.size(); "
        "assert e.engine.pool._max_overflow == b.overflow"
    )
    env = {
        **os.environ,
        # Creating the engine never connects, so no server is needed.
        "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db",
        "WORKER_THREADS": "7",
        "DB_POOL_SIZE": "500",  # retired: must have no effect
    }
    backend_root = Path(engine.__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=backend_root, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_startup_applies_the_budget_before_any_other_step():
    from api import lifecycle

    first = lifecycle._startup_steps()[0]
    assert first[0] is lifecycle._startup_thread_budget
    assert first[3] == lifecycle._ALL_ROLES and first[4] == lifecycle._PER_WORKER


async def test_apply_thread_budget_sizes_both_thread_pools():
    limiter = anyio.to_thread.current_default_thread_limiter()
    before = limiter.total_tokens
    loop = asyncio.get_running_loop()
    try:
        threads.apply_thread_budget()
        budget = engine.thread_budget()
        assert limiter.total_tokens == budget.request_threads
        assert loop._default_executor._max_workers == budget.background_threads
    finally:
        limiter.total_tokens = before


async def test_run_db_and_run_in_session_leave_the_event_loop(monkeypatch):
    database = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=database))
    loop_thread = threading.get_ident()
    seen = {}

    def query(db, value):
        seen["thread"] = threading.get_ident()
        seen["session"] = db
        return db.execute(text("SELECT :v"), {"v": value}).scalar()

    assert await threads.run_in_session(query, 7) == 7
    assert seen["thread"] != loop_thread
    # The session is closed before the result comes back: nothing is pinned.
    assert not seen["session"].in_transaction()
    assert await threads.run_db(lambda: threading.get_ident()) != loop_thread


async def test_run_in_session_closes_the_session_when_the_step_fails(monkeypatch):
    database = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=database))
    sessions = []

    def fail(db):
        db.execute(text("SELECT 1"))
        sessions.append(db)
        raise LookupError("boom")

    with pytest.raises(LookupError):
        await threads.run_in_session(fail)
    assert not sessions[0].in_transaction()
