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


def test_a_lost_read_is_handed_out_again_but_a_write_never_is(db, monkeypatch):
    from personal_agent.sessions import session_queue

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    read = submit(db, "owner", "device:" + device["id"], "chat.list", {}, "read")
    write = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "write")
    assert poll(db, device["id"], device["token"])["id"] == read["id"]
    assert poll(db, device["id"], device["token"])["id"] == write["id"]
    # Too soon: the device may still be running it.
    assert poll(db, device["id"], device["token"]) is None
    later = time.time() + session_queue.REDELIVER_AFTER + 1
    monkeypatch.setattr(session_queue.time, "time", lambda: later)
    assert poll(db, device["id"], device["token"])["id"] == read["id"]
    assert poll(db, device["id"], device["token"]) is None
    acknowledge(db, device["id"], device["token"], read["id"], {"items": []})
    assert view(db, "owner", read["id"])["status"] == "completed"


def test_heartbeat_keeps_the_device_online_and_applies_consent_once(db):
    from personal_agent.computer.devices import heartbeat
    from personal_agent.models import PersonalDevice

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions", "observe"])
    beat = heartbeat(db, device["id"], device["token"], True, True)
    assert beat["protocol"] == 2 and beat["control_enabled"] and beat["sessions_enabled"]
    assert abs(beat["server_time"] - time.time()) < 5
    row = db.get(PersonalDevice, device["id"])
    epochs = (row.epoch, row.session_epoch)
    # Restating the same consent changes nothing but liveness.
    heartbeat(db, device["id"], device["token"], True, True)
    assert (row.epoch, row.session_epoch) == epochs
    job = submit(db, "owner", "device:" + device["id"], "chat.list", {}, "hb")
    # Withdrawing revokes pending work under new epochs, once.
    heartbeat(db, device["id"], device["token"], False, False)
    assert (row.epoch, row.session_epoch) == (epochs[0] + 1, epochs[1] + 1)
    assert view(db, "owner", job["id"])["status"] == "revoked"
    heartbeat(db, device["id"], device["token"], False, False)
    assert (row.epoch, row.session_epoch) == (epochs[0] + 1, epochs[1] + 1)


def test_heartbeat_never_turns_on_sessions_the_device_was_not_paired_for(db):
    from personal_agent.computer.devices import heartbeat

    device = pair(db, "owner", "Laptop", "macos", [])
    assert heartbeat(db, device["id"], device["token"], False, True)["sessions_enabled"] is False


def test_legacy_session_consent_off_is_idempotent(db):
    from personal_agent.models import PersonalDevice

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], False)
    consent(db, device["id"], device["token"], False)
    assert db.get(PersonalDevice, device["id"]).session_epoch == 1


def test_a_revoked_device_cannot_heartbeat(db):
    from personal_agent.computer.devices import heartbeat
    from personal_agent.models import PersonalDevice

    device = pair(db, "owner", "Laptop", "macos", [])
    db.get(PersonalDevice, device["id"]).revoked = 1
    db.commit()
    with pytest.raises(LookupError):
        heartbeat(db, device["id"], device["token"], False, False)


def test_a_write_that_expired_unsent_can_be_asked_again(db, monkeypatch):
    from personal_agent.sessions import session_queue

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    first = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "same")
    later = time.time() + 120
    monkeypatch.setattr(session_queue.time, "time", lambda: later)
    # The device must look online at the later time too.
    from personal_agent.models import PersonalDevice
    db.get(PersonalDevice, device["id"]).last_seen = later
    assert view(db, "owner", first["id"])["status"] == "expired"
    again = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "same")
    assert again["id"] != first["id"] and again["status"] == "queued"


def test_a_sent_write_is_never_queued_again(db, monkeypatch):
    from personal_agent.sessions import session_queue

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    first = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "same")
    poll(db, device["id"], device["token"])
    later = time.time() + 120
    monkeypatch.setattr(session_queue.time, "time", lambda: later)
    again = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "same")
    assert again["id"] == first["id"] and again["status"] == "outcome_unknown"


def test_receipt_size_is_measured_as_utf8(db):
    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    job = submit(db, "owner", "device:" + device["id"], "chat.history", {"chat_id": "c1"}, "h")
    poll(db, device["id"], device["token"])
    # 1.5 MB of Chinese as UTF-8, 3 MB if escaped.
    text = "中" * 500_000
    assert acknowledge(db, device["id"], device["token"], job["id"], {"text": text})["accepted"]


def test_old_reads_are_pruned_but_writes_kept(db, monkeypatch):
    from personal_agent.sessions import session_queue
    from personal_agent.models import PersonalSessionOperation

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    read = submit(db, "owner", "device:" + device["id"], "chat.list", {}, "r")
    write = submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "w")
    later = time.time() + session_queue.KEEP_SECONDS + 120
    monkeypatch.setattr(session_queue.time, "time", lambda: later)
    session_queue.prune(db)
    db.expire_all()
    assert db.get(PersonalSessionOperation, read["id"]) is None
    assert db.get(PersonalSessionOperation, write["id"]) is not None


def test_a_command_no_device_took_is_withdrawn_as_not_executed(db):
    from personal_agent.computer import devices
    from personal_agent.models import PersonalDeviceCommand

    device = pair(db, "owner", "Laptop", "macos", ["observe"])
    for ident, status in (("q", "queued"), ("s", "sent")):
        db.add(PersonalDeviceCommand(id=ident, device_id=device["id"], epoch=1, work_item_id="w",
                                     action={"type": "observe"}, status=status, deadline=time.time()))
    db.commit()
    assert devices._give_up(db, "q")["not_executed"]
    assert db.get(PersonalDeviceCommand, "q").status == "revoked"
    assert devices._give_up(db, "s")["outcome_unknown"]


def test_a_run_on_the_users_computer_is_asked_about_until_it_ends(db, monkeypatch):
    from personal_agent.sessions import session_watch
    from personal_agent.models import PersonalDevice, PersonalSessionOperation
    monkeypatch.setattr(session_watch, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))

    device = pair(db, "owner", "Laptop", "macos", ["hugagent.sessions"])
    consent(db, device["id"], device["token"], True)
    send = PersonalSessionOperation(id="pa_session_send", user_id="owner", target="device:" + device["id"],
        device_id=device["id"], request_key="tool:send", operation="chat.send", args={}, work_item_id=None,
        status="completed", result={"run_id": "run_1"}, deadline=time.time())
    db.add(send)
    db.commit()
    # Offline: nothing is asked of a computer that cannot answer.
    db.get(PersonalDevice, device["id"]).last_seen = time.time() - 60
    db.commit()
    assert session_watch.device_run_ended(db, send, "run_1") is None
    assert db.query(PersonalSessionOperation).count() == 1
    consent(db, device["id"], device["token"], True)
    assert session_watch.device_run_ended(db, send, "run_1") is None
    db.expire_all()
    assert session_watch.device_run_ended(db, send, "run_1") is None  # one check at a time
    check = poll(db, device["id"], device["token"])
    assert check["operation"] == "run.status" and check["args"] == {"run_id": "run_1"}
    acknowledge(db, device["id"], device["token"], check["id"], {"status": "completed"})
    assert session_watch.device_run_ended(db, send, "run_1") == {"status": "completed"}
    text = session_watch.describe_device_run(send.target, {"status": "completed"})
    assert "finished" in text and "chat.history" in text



def test_old_commands_are_pruned_by_maintenance(db, monkeypatch):
    from personal_agent.computer import devices
    from personal_agent.models import PersonalDeviceCommand

    device = pair(db, "owner", "Laptop", "macos", ["observe"])
    db.add_all([PersonalDeviceCommand(id=ident, device_id=device["id"], epoch=1, work_item_id="w",
                                      action={"type": "observe"}, status="completed", deadline=deadline)
                for ident, deadline in (("old", time.time() - devices.COMMAND_KEEP_SECONDS - 60), ("new", time.time()))])
    db.commit()
    devices.prune_commands(db)
    db.expire_all()
    assert db.get(PersonalDeviceCommand, "old") is None and db.get(PersonalDeviceCommand, "new") is not None
