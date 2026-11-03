"""Task input reaches a running Worker through core steering, once and in order."""
import time
from types import SimpleNamespace

import pytest

from core.db.models import ChatSteerQueueItem
from personal_agent.context import AgentContext
from personal_agent.models import PersonalWorkItem
from personal_agent.worker import inputs
from tests.test_personal_agent import sessions, personal_agent_on  # noqa: F401
from tests.test_personal_agent_worker import launched, rows, steer, world  # noqa: F401


def test_late_steer_requeues_instead_of_publishing_outdated_final(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Fetch data", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        row.checkpoint = {"report": {"assignment": "run", "status": "ready", "summary": "Old answer"}}
        db.commit()
        svc.tasks.steer(row.id, "Use a script", "update")
        result = svc.tasks.complete(row.id, "Old answer", {"consumed_message_ids": [], **row.checkpoint}, owner="worker")
        assert result["status"] == "queued"
        assert result["result"] is None
        assert "report" not in row.checkpoint


def test_delivered_input_voids_an_earlier_final_report_but_keeps_progress(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        row = db.get(PersonalWorkItem, svc.tasks.submit("Fetch data", "initial")["id"])
        row.checkpoint = {"report": {"assignment": "a", "status": "ready", "summary": "Outdated"}}
        inputs.consume(row, ["m1"])
        assert "report" not in row.checkpoint and row.checkpoint["consumed_message_ids"] == ["m1"]
        row.checkpoint = {**row.checkpoint, "report": {"assignment": "a", "status": "progress", "summary": "Halfway"}}
        inputs.consume(row, ["m2"])
        assert row.checkpoint["report"]["status"] == "progress"


@pytest.mark.parametrize("change", ["cancel", "run", "expired"])
def test_execution_guard_keeps_cancellation_and_ownership(sessions, monkeypatch, change):
    from personal_agent.tasks import execution, guards
    monkeypatch.setattr(guards, "SessionLocal", sessions)
    token = execution.current_execution.set(None)
    try:
        with sessions() as db:
            svc = AgentContext(db, "owner")
            task = svc.tasks.submit("Fetch data", "initial")
            row = db.get(PersonalWorkItem, task["id"])
            row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
            db.commit()
            execution.bind(row)
            svc.tasks.steer(row.id, "Use a script", "update")
            assert guards.owned_task(row.id, "owner")["run_id"] == "run"
            # Another executor taking the Assignment over does not stop its live run's tools.
            row.owner = "other-worker"
            db.commit()
            assert guards.owned_task(row.id, "owner")["run_id"] == "run"
            if change == "cancel": row.status = "cancelled"
            if change == "run": row.run_id = "other-run"
            if change == "expired": row.lease_until = time.time() - 1
            db.commit()
            with pytest.raises(PermissionError):
                guards.owned_task(row.id, "owner")
    finally:
        execution.current_execution.reset(token)


@pytest.mark.parametrize("filename", [
    "src/backend/alembic/versions/personalsteer01_remove_task_revisions.py",
    "ce/overlay/src/backend/alembic/versions/ce_0029_personal_steer.py",
])
def test_schema_upgrade_and_downgrade_preserve_task_data(filename):
    import importlib.util
    from sqlalchemy import create_engine, text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("steer_migration", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE personal_work_items (id TEXT PRIMARY KEY, goal TEXT, revision INTEGER NOT NULL)"))
        conn.execute(text("CREATE TABLE personal_session_operations (id TEXT PRIMARY KEY, task_revision INTEGER)"))
        conn.execute(text("INSERT INTO personal_work_items VALUES ('task', 'Keep this goal', 9)"))
        conn.execute(text("INSERT INTO personal_session_operations VALUES ('operation', 9)"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert "revision" not in {c["name"] for c in inspect(conn).get_columns("personal_work_items")}
            assert conn.execute(text("SELECT goal FROM personal_work_items")).scalar() == "Keep this goal"
            module.downgrade()
            assert conn.execute(text("SELECT revision FROM personal_work_items")).scalar() == 0
            assert conn.execute(text("SELECT id FROM personal_session_operations")).scalar() == "operation"
            module.upgrade()
    engine.dispose()


@pytest.mark.asyncio
async def test_steered_attachments_are_staged_and_cumulative_limit_is_atomic(world, monkeypatch):
    from core.db.models import Artifact
    staged = []

    class Sandbox:
        async def put_file(self, box, path, content, user_id):
            staged.append(path)
    monkeypatch.setattr("personal_agent.computer.sandbox.get_personal_sandbox_provider", lambda: Sandbox())
    monkeypatch.setattr("core.files.storage.get_storage", lambda: SimpleNamespace(download_bytes=lambda key: b"file"))
    task_id, asg_id, run_id = await launched(world, "Read files", "initial")
    with world.sessions() as db:
        for i in range(12):
            db.add(Artifact(artifact_id=f"file-{i}", user_id="owner", type="other", mime_type="text/plain",
                            title="Input", filename=f"file-{i}.txt", storage_key=f"key-{i}", size_bytes=4))
        db.commit()
        svc = AgentContext(db, "owner")
        svc.tasks.steer(task_id, "First files", "first", [f"file-{i}" for i in range(6)])
        with pytest.raises(ValueError, match="ten"):
            svc.tasks.steer(task_id, "Too many files", "overflow", [f"file-{i}" for i in range(6, 12)])
        db.rollback()
        svc.tasks.steer(task_id, "Remaining files", "second", [f"file-{i}" for i in range(6, 10)])
    await world.executor.step(asg_id)
    messages = [row.message for row in sorted(rows(world, ChatSteerQueueItem, target_run_id=run_id), key=lambda r: r.steer_seq)]
    assert [m.split("\n")[0] for m in messages] == ["First files", "Remaining files"]
    assert len(staged) == 10 and all(f"/file-{i}/file-{i}.txt" in messages[-1] for i in range(10))
    await world.executor.step(asg_id)
    assert len(staged) == 10 and len(rows(world, ChatSteerQueueItem, target_run_id=run_id)) == 2


@pytest.mark.asyncio
async def test_a_backlog_larger_than_one_batch_is_delivered_in_order_without_loss(world):
    task_id = None
    with world.sessions() as db:
        task_id = AgentContext(db, "owner").tasks.submit("Process instructions", "initial")["id"]
    for i in range(35):
        steer(world, task_id, f"Instruction {i:02}", f"input-{i}")
    asg_id = world.executor.claim()
    await world.executor.launch(asg_id)
    first = world.started[-1]
    assert first.content.split("Task updates:\n")[1].split("\n\n") == [f"Instruction {i:02}" for i in range(30)]
    from tests.test_personal_agent_worker import running
    running(world, first.run.run_id)
    await world.executor.step(asg_id)
    later = sorted(rows(world, ChatSteerQueueItem, target_run_id=first.run.run_id), key=lambda r: r.steer_seq)
    assert [row.message for row in later] == [f"Instruction {i:02}" for i in range(30, 35)]
    await world.executor.step(asg_id)
    assert len(rows(world, ChatSteerQueueItem, target_run_id=first.run.run_id)) == 5


def test_late_input_requeue_retains_consumed_message_ids(sessions):
    from personal_agent.conversation.associations import instruction_messages
    with sessions() as db:
        svc = AgentContext(db, "owner")
        ident = svc.tasks.submit("Fetch data", "initial")["id"]
        task = db.get(PersonalWorkItem, ident)
        task.status, task.owner, task.run_id, task.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
        svc.tasks.steer(ident, "Already applied", "first")
        consumed = [key for key, _ in instruction_messages(db, task.chat_id, ident)]
        svc.tasks.steer(ident, "Late input", "second")
        result = svc.tasks.complete(ident, "Outdated", {"consumed_message_ids": consumed}, owner="worker")
        assert result["status"] == "queued"
        assert task.checkpoint["consumed_message_ids"] == consumed
        assert "input_artifact_ids" in task.checkpoint
