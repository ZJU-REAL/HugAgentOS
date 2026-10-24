"""Concurrency and migration regressions for durable automation admission."""
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from features.automation.admission import admit
from features.automation.models import ScheduledTaskRun
from features.automation.schema import upgrade_queue
from features.automation.service import AutomationService


def test_two_connections_cannot_consume_same_trigger(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'admit.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        svc = AutomationService(db)
        task = svc.create_task(user_id="u", task_type="prompt", prompt="run",
                               cron_expression="0 18 * * *")
        svc.request_manual_trigger(task)
        task_id = task.task_id
    rendezvous = threading.Barrier(2)
    def after_read(conn, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT scheduled_tasks.") and "EXISTS" in statement:
            rendezvous.wait(timeout=5)
    event.listen(engine, "after_cursor_execute", after_read)
    def accept():
        with factory() as db:
            try:
                return admit(db, 1)
            except OperationalError as exc:
                assert "locked" in str(exc).lower()
                return []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept), pool.submit(accept)]
            results = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", after_read)
    assert sum(map(len, results)) == 1
    with factory() as db:
        assert db.query(ScheduledTaskRun).count() == 1
        assert AutomationService(db).get_task(task_id, "u").run_count == 1
        assert admit(db, 1) == []
    engine.dispose()


def test_sqlite_queue_upgrade_retains_history_and_is_repeatable():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE scheduled_task_runs (
            run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            CONSTRAINT scheduled_task_runs_status_check CHECK
              (status IN ('running', 'success', 'failed')))"""))
        connection.execute(text("INSERT INTO scheduled_task_runs VALUES ('old', 'success')"))
        upgrade_queue(connection)
        upgrade_queue(connection)
        connection.execute(text("INSERT INTO scheduled_task_runs VALUES ('queued', 'pending')"))
        assert connection.execute(text(
            "SELECT run_id, status FROM scheduled_task_runs ORDER BY run_id"
        )).all() == [("old", "success"), ("queued", "pending")]
    engine.dispose()


@pytest.mark.parametrize("path", [
    "src/backend/alembic/versions/automationqueue01_pending.py",
    "ce/overlay/src/backend/alembic/versions/ce_0032_automation_queue.py",
])
def test_downgrade_refuses_to_erase_pending_work(path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    root = Path(__file__).resolve().parents[5]
    spec = importlib.util.spec_from_file_location("queue_migration", root / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE scheduled_task_runs (
            run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            CONSTRAINT scheduled_task_runs_status_check CHECK
              (status IN ('running', 'success', 'failed')))"""))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.execute(text("INSERT INTO scheduled_task_runs VALUES ('q', 'pending')"))
            with pytest.raises(RuntimeError, match="Drain pending"):
                module.downgrade()
            connection.execute(text("DELETE FROM scheduled_task_runs"))
            module.downgrade()
    engine.dispose()
