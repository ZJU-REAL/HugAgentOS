"""Real PostgreSQL recovery races; opt in with an isolated test database URL."""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, CreateTable, DropSchema

from personal_agent.models import PersonalAgent, PersonalWorkItem
from personal_agent.tasks import worker as module


@pytest.fixture
def recovery_db(monkeypatch):
    url = os.getenv("PA_RECOVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL via PA_RECOVERY_TEST_DATABASE_URL")
    parsed = make_url(url)
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.database == "pa_recovery_test", "Never use a business database"
    root = create_engine(url, connect_args={"options": "-c statement_timeout=5000"})
    schema = "pa_recovery_" + uuid.uuid4().hex
    with root.begin() as conn:
        conn.execute(CreateSchema(schema))
    engine = root.execution_options(schema_translate_map={None: schema})
    try:
        with engine.begin() as conn:
            # These two real ORM tables suffice for lease recovery. External
            # identity/chat FKs are unrelated to this isolated concurrency test.
            for table in (PersonalAgent.__table__, PersonalWorkItem.__table__):
                conn.execute(CreateTable(table, include_foreign_key_constraints=[]))
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(module, "SessionLocal", factory)
        monkeypatch.setattr(module.access, "allowed", lambda *args: True)
        with factory() as db:
            db.add(PersonalAgent(id="agent", user_id="owner", chat_id="chat",
                                 sandbox_session_id="box", config={}))
            db.add(PersonalWorkItem(id="task", agent_id="agent", request_key="one",
                                   goal="recover", status="running", owner="expired",
                                   run_id="old-run", lease_until=time.time() - 60,
                                   checkpoint={}))
            db.commit()
        yield factory
    finally:
        with root.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        root.dispose()


def test_second_recover_skips_task_locked_by_first(recovery_db, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = module.interrupt

    def interrupt(db, row, reason):
        calls.append(row.id)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4), "Test barrier was not released"
        return original(db, row, reason)

    monkeypatch.setattr(module, "interrupt", interrupt)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.PersonalAgentWorker().recover)
        try:
            assert entered.wait(3)
            second = pool.submit(module.PersonalAgentWorker().recover)
            second.result(timeout=3)
            assert calls == ["task"], "Two recoverers processed the same expired lease"
        finally:
            release.set()
            first.result(timeout=4)
    with recovery_db() as db:
        row = db.get(PersonalWorkItem, "task")
        assert row.status == "queued"
        assert row.checkpoint["auto_resumes"] == 1


def test_recovery_cannot_overwrite_concurrently_reclaimed_lease(recovery_db):
    # Hold the expired row while replacing its lease, as another claimant can
    # do after an earlier recovery. A stale reader must skip it, not later
    # overwrite this new execution after our commit.
    with recovery_db() as owner, ThreadPoolExecutor(max_workers=1) as pool:
        row = owner.scalar(select(PersonalWorkItem).where(
            PersonalWorkItem.id == "task").with_for_update())
        row.owner = "new-worker"
        row.run_id = "new-run"
        row.lease_until = time.time() + 90
        row.checkpoint = {"action_receipts": {"send": {"status": "uncertain"}}}
        future = pool.submit(module.PersonalAgentWorker().recover)
        skipped = False
        try:
            future.result(timeout=2)
            skipped = True
        except TimeoutError:
            pass  # Release the lock, then inspect any stale write.
        finally:
            owner.commit()
            future.result(timeout=4)
    module.PersonalAgentWorker().recover()
    with recovery_db() as db:
        row = db.get(PersonalWorkItem, "task")
        assert (row.status, row.owner, row.run_id) == ("running", "new-worker", "new-run")
        assert row.checkpoint == {"action_receipts": {"send": {"status": "uncertain"}}}


    assert skipped, "Recovery waited on a live claimant instead of skipping its lock"
