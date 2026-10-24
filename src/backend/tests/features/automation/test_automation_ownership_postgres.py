"""Opt-in ownership races in a disposable schema on a local PostgreSQL."""

from tests.features.automation.run_fixtures import running_run
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from core.db.engine import Base
from core.db.models import ChatSession, UserShadow
from features.automation.models import ScheduledTask, ScheduledTaskRun
from features.automation.runs import AutomationRunRepository, owned_run_guard
from core.runs.journal import RunJournal
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.getenv("AUTOMATION_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="local PostgreSQL opt-in required")


@pytest.fixture
def pg():
    url = make_url(POSTGRES_URL)
    assert url.host in {"127.0.0.1", "localhost", "::1"}, "Only a local test server is allowed"
    schema = "test_automation_ownership_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as db:
            db.add(UserShadow(user_id="user", username="user"))
            db.commit()
            db.add(
                ScheduledTask(
                    task_id="task", user_id="user", task_type="prompt", cron_expression="0 * * * *"
                )
            )
            db.add(ChatSession(chat_id="chat", user_id="user", title="test"))
            db.commit()
        yield sessions
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_two_recovery_claimants_cannot_both_take_the_same_snapshot(pg):
    with pg() as db:
        run_id = running_run(db, "task", "old").run_id
    barrier = threading.Barrier(2)

    def claim(token):
        with pg() as db:
            barrier.wait(timeout=5)
            return token, AutomationRunRepository(db).claim(run_id, token, expected_token="old")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["a", "b"]))
    assert sorted(accepted for _, accepted in results) == [False, True]
    winner = next(token for token, accepted in results if accepted)
    with pg() as db:
        repository = AutomationRunRepository(db)
        assert repository.complete(run_id, lease_token="old", status="success") is None
        assert repository.complete(run_id, lease_token=winner, status="success") is not None


def test_old_cancellation_waits_for_takeover_and_cannot_cancel_its_child(pg):
    with pg() as db:
        run_id = running_run(db, "task", "old").run_id
    journal = RunJournal(pg)
    child = journal.accept(
        run_id="child",
        message_id="message",
        chat_id="chat",
        user_id="user",
        request_payload={"kind": "chat"},
        recovery_snapshot={},
    )
    updated, commit = threading.Event(), threading.Event()

    def takeover():
        with pg() as db:
            db.query(ScheduledTaskRun).filter_by(run_id=run_id, lease_token="old").update(
                {ScheduledTaskRun.lease_token: "new"}
            )
            updated.set()
            assert commit.wait(5)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(takeover)
        assert updated.wait(5)
        cancellation = pool.submit(
            journal.cancel,
            child.run_id,
            reason="obsolete observer",
            cancel_guard=owned_run_guard(run_id, "old"),
        )
        commit.set()
        owner.result(timeout=5)
        assert cancellation.result(timeout=5) is False
    assert journal.cancel(child.run_id, reason="current observer", cancel_guard=owned_run_guard(run_id, "new"))


def test_completion_waiting_on_takeover_rechecks_the_token(pg):
    with pg() as db:
        run_id = running_run(db, "task", "old").run_id
    updated, commit = threading.Event(), threading.Event()

    def takeover():
        with pg() as db:
            db.query(ScheduledTaskRun).filter_by(run_id=run_id, lease_token="old").update(
                {ScheduledTaskRun.lease_token: "new"}
            )
            updated.set()
            assert commit.wait(5)
            db.commit()

    def old_complete():
        with pg() as db:
            return AutomationRunRepository(db).complete(run_id, lease_token="old", status="success")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(takeover)
        assert updated.wait(5)
        completion = pool.submit(old_complete)
        commit.set()
        owner.result(timeout=5)
        assert completion.result(timeout=5) is None
    with pg() as db:
        assert db.get(ScheduledTaskRun, run_id).lease_token == "new"
        assert db.get(ScheduledTaskRun, run_id).status == "running"


def test_additive_migration_preserves_rows_and_roundtrips_on_postgres(pg):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = (
        Path(__file__).resolve().parents[5]
        / "src/backend/alembic/versions/automationlease01_run_token.py"
    )
    spec = importlib.util.spec_from_file_location("automation_pg_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pg() as db:
        run_id = running_run(db, "task", "original").run_id
    with pg.kw["bind"].begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert conn.execute(text("SELECT status FROM scheduled_task_runs")).scalar() == "running"
        migration.upgrade()
        migration.upgrade()
        column = next(
            c
            for c in inspect(conn).get_columns("scheduled_task_runs")
            if c["name"] == "lease_token"
        )
        assert column["nullable"]
        assert column["default"] is None
    with pg() as db:
        assert db.get(ScheduledTaskRun, run_id).status == "running"
        assert db.get(ScheduledTaskRun, run_id).lease_token is None
