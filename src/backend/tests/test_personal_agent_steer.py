"""Personal task steering through the same safe boundaries as ordinary chat."""
from tests.orchestration.hook_support import make_adapter
from agentscope.message import ToolCallBlock

import time
from types import SimpleNamespace
import pytest
from agentscope.message import ToolResultState
from agentscope.tool._response import ToolChunk
from personal_agent.context import AgentContext
from personal_agent.models import PersonalWorkItem
from tests.test_personal_agent import sessions, personal_agent_on  # noqa: F401


@pytest.mark.asyncio
async def test_steer_interrupts_old_tool_and_enters_context_without_status_poll(sessions, monkeypatch):
    from personal_agent.tasks import steering
    monkeypatch.setattr(steering, "SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Browse website", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
        inbox = steering.TaskSteering(row.id, "owner", "run", "worker", [])
        svc.tasks.steer(row.id, "Use a script instead", "update")
    middleware = make_adapter(*inbox.hook_policy().specs())
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run", context=[], steer_delivery=None))
    called = []
    async def tool(**kwargs):
        called.append(True)
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)
    results = [x async for x in middleware.on_acting(agent, {"tool_call": ToolCallBlock(id="steer-tool", name="test_tool", input="{}")}, tool)]
    assert not called
    assert results[-1].state == ToolResultState.INTERRUPTED
    async def model(**kwargs):
        assert "Use a script instead" in agent.state.context[-1].content[0].text
        yield "continued"
    assert [x async for x in middleware.on_reasoning(agent, {}, model)] == ["continued"]
    results = [x async for x in middleware.on_acting(agent, {"tool_call": ToolCallBlock(id="steer-tool", name="test_tool", input="{}")}, tool)]
    assert called == [True]
    assert results[-1].state == ToolResultState.SUCCESS


@pytest.mark.asyncio
async def test_inflight_result_is_preserved_and_multiple_inputs_are_delivered_once(sessions, monkeypatch):
    from personal_agent.tasks import steering
    monkeypatch.setattr(steering, "SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Fetch data", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
    inbox = steering.TaskSteering(task["id"], "owner", "run", "worker", [])
    middleware = make_adapter(*inbox.hook_policy().specs())
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run", context=[], steer_delivery=None))
    async def tool(**kwargs):
        with sessions() as db:
            svc = AgentContext(db, "owner")
            svc.tasks.steer(task["id"], "Use a script", "one")
            svc.tasks.steer(task["id"], "Include the source", "two")
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)
    results = [x async for x in middleware.on_acting(agent, {"tool_call": ToolCallBlock(id="steer-tool", name="test_tool", input="{}")}, tool)]
    assert results[-1].state == ToolResultState.SUCCESS
    async def model(**kwargs):
        text = agent.state.context[-1].content[0].text
        assert text.index("Use a script") < text.index("Include the source")
        yield "answer"
    assert [x async for x in middleware.on_reasoning(agent, {}, model)] == ["answer"]
    assert await inbox.take("run") is None
    with sessions() as db:
        result = AgentContext(db, "owner").tasks.complete(
            task["id"], "Done", {"consumed_message_ids": sorted(inbox.consumed)}, owner="worker")
        assert result["status"] == "completed"
        assert "revision" not in result


def test_late_steer_requeues_instead_of_publishing_outdated_final(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Fetch data", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        row.checkpoint = {"final_answer": {"run_id": "run", "text": "Old answer"}}
        db.commit()
        svc.tasks.steer(row.id, "Use a script", "update")
        result = svc.tasks.complete(row.id, "Old answer", {"consumed_message_ids": []}, owner="worker")
        assert result["status"] == "queued"
        assert result["result"] is None
        assert "final_answer" not in row.checkpoint


@pytest.mark.parametrize("change", ["cancel", "owner", "run", "expired"])
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
            if change == "cancel": row.status = "cancelled"
            if change == "owner": row.owner = "other-worker"
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
async def test_steer_invalidates_an_earlier_finish_before_model_continues(sessions, monkeypatch):
    from personal_agent.tasks import steering
    monkeypatch.setattr(steering, "SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Fetch data", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        row.checkpoint = {"final_answer": {"run_id": "run", "text": "Outdated"}}
        db.commit()
        svc.tasks.steer(row.id, "Use a script", "update")
    inbox = steering.TaskSteering(task["id"], "owner", "run", "worker", [])
    middleware = make_adapter(*inbox.hook_policy().specs())
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run", context=[], steer_delivery=None))
    async def model(**kwargs):
        with sessions() as db:
            assert "final_answer" not in db.get(PersonalWorkItem, task["id"]).checkpoint
        yield "Revised answer"
    assert [x async for x in middleware.on_reasoning(agent, {}, model)] == ["Revised answer"]


@pytest.mark.asyncio
async def test_steered_attachments_are_staged_and_cumulative_limit_is_atomic(sessions, monkeypatch):
    from core.db.models import Artifact
    from personal_agent.tasks import steering
    monkeypatch.setattr(steering, "SessionLocal", sessions)
    monkeypatch.setattr("core.db.engine.SessionLocal", sessions)
    staged = []
    class Sandbox:
        async def put_file(self, box, path, content, user_id):
            staged.append(path)
    monkeypatch.setattr("personal_agent.computer.sandbox.get_personal_sandbox_provider", lambda: Sandbox())
    monkeypatch.setattr('core.files.storage.get_storage', lambda: SimpleNamespace(download_bytes=lambda key: b"file"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Read files", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        for i in range(12):
            db.add(Artifact(artifact_id=f"file-{i}", user_id="owner", type="other", mime_type="text/plain",
                            title="Input", filename=f"file-{i}.txt", storage_key=f"key-{i}", size_bytes=4))
        db.commit()
        svc.tasks.steer(row.id, "First files", "first", [f"file-{i}" for i in range(6)])
        with pytest.raises(ValueError, match="ten"):
            svc.tasks.steer(row.id, "Too many files", "overflow", [f"file-{i}" for i in range(6, 12)])
        db.rollback()
        svc.tasks.steer(row.id, "Remaining files", "second", [f"file-{i}" for i in range(6, 10)])
    inbox = steering.TaskSteering(task["id"], "owner", "run", "worker", [])
    delivery = await inbox.take("run")
    assert "Too many files" not in delivery["message"]
    assert len(staged) == 10
    assert all(f"/file-{i}/file-{i}.txt" in delivery["message"] for i in range(10))
    await inbox.acknowledge(delivery)
    assert await inbox.take("run") is None


@pytest.mark.asyncio
async def test_startup_backlog_does_not_acknowledge_inputs_omitted_from_initial_prompt(sessions, monkeypatch):
    from personal_agent.tasks import steering
    from personal_agent.conversation.associations import instruction_messages
    monkeypatch.setattr(steering, "SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Process instructions", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
        for i in range(35):
            svc.tasks.steer(row.id, f"Instruction {i:02}", f"input-{i}")
        messages = instruction_messages(db, svc.identity.agent().chat_id, row.id)
    inbox = steering.TaskSteering(task["id"], "owner", "run", "worker", messages)
    assert len(inbox.initial_messages) == 30
    delivery = await inbox.take("run")
    assert delivery["message"].split("\n\n") == [f"Instruction {i:02}" for i in range(30, 35)]
    await inbox.acknowledge(delivery)
    assert await inbox.take("run") is None


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
