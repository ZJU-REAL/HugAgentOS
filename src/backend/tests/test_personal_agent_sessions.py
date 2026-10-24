"""Public session bridge contract: consent, isolation, receipts and replay."""

import time
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.db.engine import Base
from core.db.models import UserShadow
from personal_agent.computer.devices import pair
from personal_agent.sessions.session_queue import submit, poll, acknowledge, consent, view


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add_all(
            [UserShadow(user_id=u, user_center_id=u, username=u) for u in ("owner", "other")]
        )
        session.commit()
        yield session
    engine.dispose()


def test_consent_is_separate_and_operations_are_not_replayed(db):
    device = pair(db, "owner", "Laptop", "windows", ["hugagent.sessions"])
    with pytest.raises(ValueError):
        submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "key")
    consent(db, device["id"], device["token"], True)
    first = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "key")
    assert (
        submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "key")["id"]
        == first["id"]
    )
    with pytest.raises(ValueError):
        submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "B"}, "key")
    command = poll(db, device["id"], device["token"])
    assert command["id"] == first["id"] and poll(db, device["id"], device["token"]) is None
    with pytest.raises(LookupError):
        view(db, "other", first["id"])
    acknowledge(db, device["id"], device["token"], first["id"], {"chat_id": "local"})
    assert view(db, "owner", first["id"])["result"] == {"chat_id": "local"}
    assert acknowledge(db, device["id"], device["token"], first["id"], {"chat_id": "local"})[
        "accepted"
    ]
    with pytest.raises(ValueError):
        acknowledge(db, device["id"], device["token"], first["id"], {"chat_id": "changed"})


def test_revoke_and_deadline_fence_pending_work(db):
    device = pair(db, "owner", "Laptop", "linux", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    job = submit(db, "owner", "device:" + device["id"], "chat.list", {}, "list")
    consent(db, device["id"], device["token"], False)
    assert view(db, "owner", job["id"])["status"] == "revoked"
    assert poll(db, device["id"], device["token"]) is None
    consent(db, device["id"], device["token"], True)
    with pytest.raises(LookupError):
        submit(db, "other", "device:" + device["id"], "chat.list", {}, "other")
    with pytest.raises(ValueError):
        submit(db, "owner", "device:" + device["id"], "shell.exec", {}, "bad")


def test_transport_rejects_malformed_server():
    from hugagent_cli.transport import Client

    with pytest.raises(ValueError):
        Client("http://127.0.0.1:3001/api?host=remote", "secret")
    with pytest.raises(ValueError):
        Client("https://user:pass@remote.example/api", "secret")


def test_queued_task_operations_are_revoked_when_task_execution_changes(db):
    from personal_agent.models import PersonalWorkItem
    from personal_agent.context import AgentContext
    from personal_agent.sessions.session_queue import check

    device = pair(db, "owner", "Laptop", "linux", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    task = AgentContext(db, "owner").tasks.submit("Manage sessions", "task")["id"]
    row = db.get(PersonalWorkItem, task)
    row.status = "running"
    row.owner = "worker"
    row.run_id = "run"
    row.lease_until = time.time() + 100
    db.commit()
    first = submit(
        db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "A", task, "run"
    )
    row.run_id = "replacement-run"
    db.commit()
    assert poll(db, device["id"], device["token"]) is None
    assert view(db, "owner", first["id"])["status"] == "revoked"
    row.run_id = "run"
    db.commit()
    second = submit(
        db, "owner", "device:" + device["id"], "chat.create", {"title": "B"}, "B", task, "run"
    )
    assert poll(db, device["id"], device["token"])["id"] == second["id"]
    row.run_id = "replacement-run"
    db.commit()
    with pytest.raises(LookupError):
        check(db, device["id"], device["token"], second["id"])


async def test_task_grants_are_bound_to_run_owner_and_running_lease(db, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from starlette.requests import Request
    from fastapi import HTTPException
    from core.infra.ephemeral import LocalEphemeralState
    from personal_agent.sessions import session_auth
    from personal_agent.tools import tools
    from personal_agent.models import PersonalWorkItem
    from personal_agent.context import AgentContext

    store = LocalEphemeralState()
    monkeypatch.setattr(session_auth, "get_ephemeral_state", lambda: store)
    from core.db import engine as db_engine

    monkeypatch.setattr(
        db_engine, "SessionLocal", sessionmaker(bind=db.bind, expire_on_commit=False)
    )
    monkeypatch.setattr(tools, "SessionLocal", sessionmaker(bind=db.bind, expire_on_commit=False))
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessionmaker(bind=db.bind, expire_on_commit=False))
    task = AgentContext(db, "owner").tasks.submit("Read sessions", "grant")["id"]
    row = db.get(PersonalWorkItem, task)
    row.status = "running"
    row.owner = "worker"
    row.run_id = "run"
    row.lease_until = time.time() + 100
    db.commit()
    token = await session_auth.mint(task, "owner", 30)
    request = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    user, grant = await session_auth.resolve(request)
    assert user.user_id == "owner" and grant["task"] == task
    row.run_id = "replacement-run"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        await session_auth.resolve(request)
    assert exc.value.status_code == 401
    row.run_id = "run"
    db.commit()
    row.owner = "replacement"
    db.commit()
    with pytest.raises(HTTPException):
        await session_auth.resolve(request)
    row.owner = "worker"
    row.lease_until = time.time() - 1
    db.commit()
    with pytest.raises(HTTPException):
        await session_auth.resolve(request)
    await store.drop(session_auth.key(token))
    with pytest.raises(HTTPException):
        await session_auth.resolve(request)


def test_trusted_cloud_installer_script_is_valid_python():
    from personal_agent.computer.sandbox import SANDBOX_PYTHON
    from personal_agent.sessions.session_cli_install import SCRIPT

    compile(SCRIPT.replace("{python}", SANDBOX_PYTHON), "trusted-cli-installer", "exec")




def test_project_operations_map_to_fixed_endpoints():
    from hugagent_cli.contract import READS, endpoint, validate

    assert {"project.list", "project.get", "project.chats"} <= READS
    assert endpoint("project.list", {"q": "Alpha", "page": 1}) == (
        "GET",
        "/v1/projects?q=Alpha&page=1",
        None,
    )
    assert endpoint("project.chats", {"project_id": "proj_1", "page_size": 5}) == (
        "GET",
        "/v1/projects/proj_1/chats?page_size=5",
        None,
    )
    assert endpoint("chat.create", {"title": "T", "project_id": "proj_1"}) == (
        "POST",
        "/v1/chats",
        {"title": "T", "project_id": "proj_1"},
    )
    assert endpoint("chat.send", {"chat_id": "c1", "message": "m"})[2] == {
        "chat_id": "c1",
        "message": "m",
        "stream": True,
    }
    # A chat keeps its own project; sending never re-targets it.
    for operation, bad in (
        ("project.get", {"project_id": "../x"}),
        ("project.get", {}),
        ("chat.send", {"chat_id": "c1", "message": "m", "project_id": "proj_1"}),
    ):
        with pytest.raises(ValueError):
            validate(operation, bad)


def test_send_runs_in_the_chats_own_project(db):
    from core.chat.operations.admission import _check_project
    from core.chat.schemas import ChatRequest
    from core.db.models import ChatSession, Project

    db.add_all(
        [
            Project(project_id="proj_1", name="P", kind="personal", owner_user_id="owner"),
            Project(project_id="proj_2", name="Q", kind="personal", owner_user_id="owner"),
            ChatSession(chat_id="chat_a", user_id="owner", title="A", project_id="proj_1"),
        ]
    )
    db.commit()
    for sent in (None, "proj_2"):
        request = ChatRequest(chat_id="chat_a", message="hi", project_id=sent)
        _check_project(db, request, "owner")
        assert request.project_id == "proj_1"
    foreign = ChatRequest(chat_id="chat_a", message="hi")
    _check_project(db, foreign, "other")
    assert foreign.project_id is None


def test_private_network_http_is_allowed_by_default_and_can_be_disabled(monkeypatch):
    from hugagent_cli.transport import Client

    monkeypatch.delenv("HUGAGENT_ALLOW_HTTP", raising=False)
    assert Client("http://10.0.0.5/api", "secret").server == "http://10.0.0.5/api"
    monkeypatch.setenv("HUGAGENT_ALLOW_HTTP", "0")
    with pytest.raises(ValueError):
        Client("http://10.0.0.5/api", "secret")
    assert Client("https://10.0.0.5/api", "secret")
    assert Client("http://backend:3001", "secret")
