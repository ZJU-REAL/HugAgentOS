"""Queue admission by separate OS processes and migration with real PostgreSQL."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from features.automation.models import ScheduledTask, ScheduledTaskRun
from core.infra.time import utc_now
from tests.features.automation.test_automation_ownership_postgres import pg, POSTGRES_URL

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="local PostgreSQL opt-in required")


def test_processes_admit_each_trigger_once(pg, tmp_path):
    with pg() as db:
        task = db.get(ScheduledTask, "task")
        task.manual_trigger_at = utc_now()
        db.commit()
        schema = db.execute(text("SELECT current_schema()")).scalar()
    script = """
import json, os, time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from features.automation.admission import admit
engine=create_engine(os.environ['AUTOMATION_TEST_POSTGRES_URL'],
 connect_args={'options':'-csearch_path='+os.environ['QUEUE_TEST_SCHEMA']})
barrier=Path(os.environ['QUEUE_TEST_BARRIER'])
(barrier/str(os.getpid())).touch()
while not (barrier/'release').exists(): time.sleep(.01)
with sessionmaker(engine)() as db: print('RESULT:'+json.dumps(admit(db,8)),flush=True)
"""
    env = {**os.environ, "QUEUE_TEST_SCHEMA": schema, "QUEUE_TEST_BARRIER": str(tmp_path)}
    processes = []
    accepted = []
    try:
        for _ in range(4):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 30
        while len(list(tmp_path.iterdir())) < 4:
            assert all(process.poll() is None for process in processes)
            assert time.monotonic() < deadline, "admission workers failed to reach barrier"
            time.sleep(.01)
        (tmp_path / "release").touch()
        for process in processes:
            output, error = process.communicate(timeout=30)
            assert process.returncode == 0, error
            accepted.extend(json.loads(next(
                line[7:] for line in output.splitlines() if line.startswith("RESULT:")
            )))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.communicate()
    assert len(accepted) == 1
    with pg() as db:
        assert db.query(ScheduledTaskRun).count() == 1
        assert db.get(ScheduledTask, "task").run_count == 1


@pytest.mark.parametrize("path", [
    "src/backend/alembic/versions/automationqueue01_pending.py",
    "ce/overlay/src/backend/alembic/versions/ce_0032_automation_queue.py",
])
def test_queue_migration_keeps_history_and_refuses_pending_downgrade(pg, path):
    root = Path(__file__).resolve().parents[5]
    spec = importlib.util.spec_from_file_location("queue_migration", root/path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pg.kw["bind"].begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            conn.execute(text("INSERT INTO scheduled_task_runs (run_id,task_id,status,started_at) VALUES ('history','task','success',now())"))
            migration.upgrade()
            migration.upgrade()
            conn.execute(text("INSERT INTO scheduled_task_runs (run_id,task_id,status,started_at) VALUES ('pending','task','pending',now())"))
            with pytest.raises(RuntimeError, match="Drain pending"):
                migration.downgrade()
            conn.execute(text("UPDATE scheduled_task_runs SET status='success' WHERE run_id='pending'"))
            migration.downgrade()
            assert conn.execute(text("SELECT count(*) FROM scheduled_task_runs")).scalar() == 2
            migration.upgrade()
