"""The typed session tool: built from the contract, read-only in turns, gated writes in tasks,
and the watcher that reports the end of runs a task started."""

import json
import time
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, Project, UserShadow
from hugagent_cli.contract import READS, ROUTES
from personal_agent.tasks import approvals
from personal_agent.sessions import session_cloud
from personal_agent.sessions import session_tool
from personal_agent.sessions import session_watch
from personal_agent.tools import tools
from personal_agent.models import PersonalMessageInbox, PersonalSessionOperation as Operation, PersonalWorkItem
from personal_agent.context import AgentContext


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (approvals, session_tool, session_watch, tools):
        monkeypatch.setattr(module, "SessionLocal", factory)
        monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", factory)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    with factory() as session:
        session.add(UserShadow(user_id="owner", user_center_id="owner", username="owner"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def cloud(monkeypatch):
    calls = []

    async def execute(user, operation, args, origin=None):
        calls.append((operation, args))
        if operation == "chat.send":
            return {"run_id": "run_x", "chat_id": args["chat_id"], "status": "accepted"}
        return {"items": [{"name": "测试"}]}

    monkeypatch.setattr(session_cloud, "execute", execute)
    return calls


def running_task(db, key="task"):
    task_id = AgentContext(db, "owner").tasks.submit("Manage sessions", key)["id"]
    row = db.get(PersonalWorkItem, task_id)
    row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 100
    db.commit()
    return task_id


def text(chunk):
    return json.loads(chunk.content[0].text)


def test_operations_come_from_the_contract():
    assert set(session_tool.operations(True)[1:]) == set(ROUTES)
    assert set(session_tool.operations(False)[1:]) == READS
    reads_only = session_tool.build("owner")
    assert set(reads_only.__annotations__["operation"].__args__) == {"device.list", *READS}
    assert "chat.send" not in reads_only.__doc__ and "chat.send" in session_tool.build("owner", "t").__doc__


async def test_a_turn_reads_at_once_and_cannot_write(db, cloud):
    tool = session_tool.build("owner")
    result = text(await tool("project.list", {"q": "测试"}))
    assert result["status"] == "completed" and result["result"]["items"][0]["name"] == "测试"
    refused = text(await tool("chat.create", {"title": "T"}))
    assert "background task" in refused["error"]
    assert cloud == [("project.list", {"q": "测试"})]
    assert db.scalar(select(Operation).where(Operation.operation == "chat.create")) is None


async def test_a_task_write_waits_for_approval_and_reuses_its_receipt(db, cloud):
    task_id = running_task(db)
    tool = session_tool.build("owner", task_id)
    pending = text(await tool("chat.send", {"chat_id": "chat_a", "message": "go"}))
    assert pending["pending_approval"] and cloud == []
    row = db.get(PersonalWorkItem, task_id)
    db.refresh(row)
    action = row.checkpoint["pending_approval"]["action"]
    assert action["operation"] == "chat.send" and action["request_key"].startswith(session_tool.KEY_PREFIX)
    # The same action keeps one key in the task, so an approval matches the retried call.
    assert session_tool.request_key(task_id, "cloud", "chat.send", {"chat_id": "chat_a", "message": "go"}) == action["request_key"]
    assert session_tool.request_key(task_id, "cloud", "chat.list", {}) != session_tool.request_key(task_id, "cloud", "chat.list", {})


async def test_watcher_reports_a_finished_run_once(db, cloud):
    task_id = running_task(db)
    db.add_all([
        Project(project_id="prj_1", name="测试", kind="personal", owner_user_id="owner"),
        ChatSession(chat_id="chat_a", user_id="owner", title="写诗", project_id="prj_1"),
    ])
    db.flush()
    db.add_all([
        ChatMessage(message_id="msg_a", chat_id="chat_a", role="assistant", content="床前明月光", chat_seq=2),
        ChatRun(run_id="run_x", chat_id="chat_a", user_id="owner", message_id="msg_a", status="running"),
        Operation(id="op_1", user_id="owner", target="cloud", request_key=session_tool.KEY_PREFIX + "k", operation="chat.send",
            args={"chat_id": "chat_a", "message": "go"}, work_item_id=task_id, epoch=1, status="completed",
            result={"run_id": "run_x", "chat_id": "chat_a"}, deadline=time.time() + 60),
    ])
    db.commit()
    events = lambda: db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.request_key == "run:run_x")).all()
    session_watch.report_finished()
    assert events() == []  # the run is still working
    run = db.get(ChatRun, "run_x")
    run.status = "completed"
    db.commit()
    session_watch.report_finished()
    assert events() == []  # the task is still working and may follow the run itself
    task = db.get(PersonalWorkItem, task_id)
    task.status = "done"
    db.commit()
    session_watch.report_finished()
    session_watch.report_finished()
    [inbox] = events()
    assert inbox.payload["task_id"] == task_id and inbox.payload["event"] == session_watch.EVENT
    event = db.get(ChatMessage, inbox.message_id)
    assert "写诗" in event.content and "测试" in event.content and "床前明月光" in event.content
    state = session_watch.state_block(db, "owner")
    assert "写诗" in state and '"project": "测试"' in state and '"run_status": "completed"' in state


async def test_a_run_the_task_followed_is_not_reported_again(db, cloud, monkeypatch):
    task_id = running_task(db)
    db.add(ChatSession(chat_id="chat_a", user_id="owner", title="A"))
    db.flush()
    db.add_all([
        ChatRun(run_id="run_x", chat_id="chat_a", user_id="owner", message_id="msg_none", status="completed"),
        Operation(id="op_1", user_id="owner", target="cloud", request_key=session_tool.KEY_PREFIX + "k", operation="chat.send",
            args={"chat_id": "chat_a", "message": "go"}, work_item_id=task_id, epoch=1, status="completed",
            result={"run_id": "run_x", "chat_id": "chat_a"}, deadline=time.time() + 60),
    ])
    db.commit()

    async def execute(user, operation, args, origin=None):
        return {"run_id": args["run_id"], "status": "completed"}

    monkeypatch.setattr(session_cloud, "execute", execute)
    result = text(await session_tool.build("owner", task_id)("run.status", {"run_id": "run_x"}, wait_seconds=30))
    assert result["result"]["status"] == "completed"
    task = db.get(PersonalWorkItem, task_id)
    db.refresh(task)
    assert task.checkpoint["observed_runs"] == ["run_x"]
    task.status = "done"
    db.commit()
    session_watch.report_finished()
    assert db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.request_key == "run:run_x")) is None


async def test_runs_started_another_way_are_not_watched(db):
    task_id = running_task(db)
    db.add(ChatSession(chat_id="chat_a", user_id="owner", title="A"))
    db.flush()
    db.add_all([
        ChatRun(run_id="run_cli", chat_id="chat_a", user_id="owner", message_id="m", status="completed"),
        Operation(id="op_cli", user_id="owner", target="cloud", request_key="cli-key", operation="chat.send",
            args={"chat_id": "chat_a", "message": "go"}, work_item_id=task_id, epoch=1, status="completed",
            result={"run_id": "run_cli", "chat_id": "chat_a"}, deadline=time.time() + 60),
    ])
    db.get(PersonalWorkItem, task_id).status = "done"
    db.commit()
    session_watch.report_finished()
    assert db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.request_key == "run:run_cli")) is None


def test_the_tool_passes_the_personal_profiles_filter():
    # Both Personal Agent profiles keep only tools named pa_*.
    assert session_tool.build("owner").__name__.startswith("pa_")
    assert session_tool.build("owner", "t").__name__.startswith("pa_")
