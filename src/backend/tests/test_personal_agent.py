"""Personal Agent public service seams: ownership, admission and final-only history."""
import personal_agent.tasks.guards as _pa_guards
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.engine import Base
from core.db.models import UserShadow
from personal_agent.context import AgentContext

@pytest.fixture(autouse=True)
def personal_agent_on(monkeypatch):
    """These tests exercise the module itself: it is switched on and every user may use it."""
    from core.harness.registry import registry
    from personal_agent import access

    monkeypatch.setattr(registry.registration_state(), 'enabled', {*registry.registration_state().enabled, access.NAME})
    monkeypatch.setattr(access, "user_has_capability", lambda db, user_id, flag: True)


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    # A file database: background steps run in worker threads, each on its own connection.
    engine = create_engine(f"sqlite:///{tmp_path / 'pa.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Async code reaches the database through core.db.threads, which opens core.db.engine sessions.
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    with factory() as db:
        for uid in ("owner", "other"):
            db.add(UserShadow(user_id=uid, user_center_id=uid, username=uid))
        db.commit()
    yield factory
    engine.dispose()

def test_single_lifetime_session_and_idempotent_task(sessions):
    with sessions() as db:
        service = AgentContext(db, "owner")
        one = service.identity.ensure()
        assert service.identity.ensure()["chat_id"] == one["chat_id"]
        first = service.tasks.submit("Prepare file", "request-one")
        assert service.tasks.submit("Prepare file", "request-one")["id"] == first["id"]
        service.tasks.submit("Search independently", "request-two")
        assert len(service.tasks.list()) == 2
        assert all(m["role"] == "user" for m in service.conversation.list())

def test_other_user_cannot_cancel_or_steer_task(sessions):
    with sessions() as db:
        owner = AgentContext(db, "owner")
        task = owner.tasks.submit("Prepare file", "request-one")
        other = AgentContext(db, "other")
        with pytest.raises(LookupError):
            other.tasks.cancel(task["id"])
        with pytest.raises(LookupError):
            other.tasks.steer(task["id"], "Change file", "request-two")

def test_final_result_saved_once_and_internal_steps_not_exposed(sessions):
    with sessions() as db:
        service = AgentContext(db, "owner")
        task = service.tasks.submit("Prepare file", "request-one")
        service.tasks.complete(task["id"], "File ready", {"thinking": "private", "tools": ["write"]})
        service.tasks.complete(task["id"], "File ready", {})
        history = service.conversation.list()
        # The result goes to the agent once; it tells the user in its next turn.
        from personal_agent.conversation.message_routing import pending
        assert [m["content"] for m in history] == ["Prepare file"] and len(pending(service)) == 1
        assert "thinking" not in str(history)
        assert "tools" not in str(history)
        assert service.tasks.list()[0]["status"] == "completed"


def test_public_chat_list_excludes_lifetime_session_and_cannot_delete(sessions):
    from core.db.repository.chat import ChatSessionRepository
    with sessions() as db:
        service = AgentContext(db, "owner")
        agent = service.identity.ensure()
        repo = ChatSessionRepository(db)
        assert repo.list_by_user("owner")[1] == 0
        with pytest.raises(ValueError): repo.soft_delete(agent["chat_id"])


def test_expired_worker_cannot_publish_result(sessions):
    import time
    from personal_agent.models import PersonalWorkItem
    with sessions() as db:
        service = AgentContext(db, "owner")
        task = service.tasks.submit("One task", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.lease_until = "running", "worker-one", time.time() - 1
        db.commit()
        with pytest.raises(ValueError): service.tasks.complete(row.id, "Outdated result", {}, owner="worker-one")
        assert len(service.conversation.list()) == 1


def test_exact_approval_and_receipt_never_dispatch_twice(sessions, monkeypatch):
    from personal_agent.tasks import approvals
    from personal_agent.models import PersonalWorkItem
    monkeypatch.setattr(approvals, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    from personal_agent.tools import tools
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        service = AgentContext(db, "owner")
        task = service.tasks.submit("Send file", "one")
        row = db.get(PersonalWorkItem, task["id"])
        import time
        row.status, row.lease_until = "running", time.time() + 90
        db.commit()
        action = {"kind": "email.send", "to": "test@example.test", "attachment_sha": "original"}
        digest, pending = approvals.authorize(row.id, "owner", action)
        assert pending["pending_approval"]
        db.refresh(row); row.status = "waiting"; db.commit()
        with pytest.raises(ValueError): approvals.approve(service, row.id, "0" * 64, True)
        approvals.approve(service, row.id, digest, True)
        assert row.status == "queued" and row.owner is None
        row.status, row.lease_until = "running", time.time() + 90; db.commit()
        assert approvals.authorize(row.id, "owner", action) == (digest, None)
        assert approvals.authorize(row.id, "owner", action)[1]["status"] == "uncertain"
        approvals.receipt(row.id, "owner", digest, {"status": "sent"})
        assert approvals.authorize(row.id, "owner", action)[1]["status"] == "sent"
        assert approvals.authorize(row.id, "owner", {**action, "attachment_sha": "changed"})[1]["pending_approval"]


def test_approval_mode_skips_only_allowed_actions(sessions, monkeypatch):
    import time
    from personal_agent.tasks import approvals
    from personal_agent.tools import tools
    from personal_agent.models import PersonalWorkItem
    monkeypatch.setattr(approvals, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        service = AgentContext(db, "owner")
        task = service.tasks.submit("Use the computer", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.lease_until = "running", time.time() + 90
        db.commit()
        click = {"kind": "computer.action", "target": "cloud", "action": {"type": "click", "x": 1, "y": 2}}
        delete = {"kind": "hugagent.session", "target": "cloud", "operation": "chat.delete", "args": {"chat_id": "c"}}
        email = {"kind": "email.send", "to": "test@example.test"}
        with pytest.raises(ValueError): service.identity.configure({"approval_mode": "never"})
        service.identity.configure({"approval_mode": "auto"})
        assert approvals.authorize(row.id, "owner", click)[1] is None
        assert approvals.authorize(row.id, "owner", delete)[1]["pending_approval"]
        db.refresh(row); row.checkpoint = {}; db.commit()
        assert approvals.authorize(row.id, "owner", email)[1]["pending_approval"]
        db.refresh(row); row.checkpoint = {}; db.commit()
        service.identity.configure({"approval_mode": "full"})
        assert approvals.authorize(row.id, "owner", email)[1] is None

def test_device_credentials_are_scoped_and_command_is_delivered_once(sessions):
    import time
    from personal_agent.computer.devices import pair, poll, acknowledge, authenticate, consent
    from personal_agent.models import PersonalDeviceCommand
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Observe device", "one")
        from personal_agent.models import PersonalWorkItem
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.lease_until = "running", time.time() + 90
        db.commit()
        paired = pair(db, "owner", "Test computer", "linux", ["observe"])
        consent(db, paired["id"], paired["token"], True)
        with pytest.raises(LookupError): authenticate(db, paired["id"], "wrong")
        command = PersonalDeviceCommand(id="cmd", device_id=paired["id"], epoch=1,
            work_item_id=task["id"], action={"type": "observe"}, deadline=time.time() + 30)
        db.add(command); db.commit()
        assert poll(db, paired["id"], paired["token"])["id"] == "cmd"
        assert poll(db, paired["id"], paired["token"]) is None
        assert acknowledge(db, paired["id"], paired["token"], "cmd", {"observation_id": "one"})["accepted"]
        assert acknowledge(db, paired["id"], paired["token"], "cmd", {})["accepted"]
        device = authenticate(db, paired["id"], paired["token"])
        device.revoked = 1; db.commit()
        with pytest.raises(LookupError): poll(db, paired["id"], paired["token"])


@pytest.mark.asyncio
async def test_multiple_tasks_execute_in_background_with_one_canonical_session(sessions, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from personal_agent.tasks import worker as module
    from personal_agent.models import PersonalWorkItem
    from core.db.models import ChatSession
    from sqlalchemy import select, func
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    @asynccontextmanager
    async def binding(**kwargs):
        yield SimpleNamespace(run_id=kwargs["binding_run_id"], owner="test")
    monkeypatch.setattr(module, "durable_run_binding", binding)
    barrier, started = asyncio.Event(), []
    async def execute(ident, _):
        started.append(ident)
        await barrier.wait()
        return "Task complete: " + ident, {}
    worker = module.PersonalAgentWorker(execute=execute)
    with sessions() as db:
        service = AgentContext(db, "owner")
        for i in range(4): service.tasks.submit("Independent task " + str(i), str(i))
    active = []
    for _ in range(3):
        ident = worker.claim()
        active.append(asyncio.create_task(worker.run(ident)))
    await asyncio.sleep(.01)
    assert len(started) == 3
    assert worker.claim() is None  # Per-owner cap applies to independently claimed tasks.
    barrier.set()
    await asyncio.gather(*active)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChatSession)) == 5
        assert len({row.chat_id for row in db.scalars(select(PersonalWorkItem))}) == 4
        service = AgentContext(db, "owner")
        assert sum(t["status"] == "completed" for t in service.tasks.list()) == 3
        from personal_agent.conversation.message_routing import pending
        assert len(pending(service)) == 3  # One report turn per finished task.
    assert worker.claim() is not None


def test_api_admission_returns_202_and_final_only_projection(sessions):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from personal_agent.api.agent import router
    app = FastAPI(); app.include_router(router)
    def database():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="owner", user_center_id="owner", username="owner")
    with TestClient(app) as client:
        init = client.post("/v1/personal-agent/initialize")
        assert init.status_code == 200
        response = client.post("/v1/personal-agent/messages", json={"content": "Create file", "request_key": "api-one"})
        assert response.status_code == 202
        ident = response.json()["data"]["message_id"]
        assert client.post("/v1/personal-agent/messages", json={"content": "Create file", "request_key": "api-one"}).json()["data"]["message_id"] == ident
        assert len(client.get("/v1/personal-agent/messages").json()["data"]) == 1
        app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="other", user_center_id="other", username="other")
        assert client.post("/v1/personal-agent/work-items/" + ident + "/cancel").status_code == 404


def test_expired_execution_cannot_use_tools_or_dispatch_approval(sessions, monkeypatch):
    import time
    from personal_agent.tools import tools
    from personal_agent.tasks import approvals
    from personal_agent.tasks.execution import current_execution, bind
    from personal_agent.models import PersonalWorkItem
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(approvals, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Prepare output", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.run_id, row.owner, row.lease_until = "running", "run-one", "worker-one", time.time()+90
        db.commit(); bind(row)
        assert _pa_guards.owned_task(row.id, "owner")["run_id"] == "run-one"
        row.run_id, row.owner = "run-two", "worker-two"; db.commit()
        with pytest.raises(PermissionError): _pa_guards.owned_task(row.id, "owner")
        with pytest.raises(PermissionError): approvals.authorize(row.id, "owner", {"kind": "send"})
        current_execution.set(None)
        row.lease_until = time.time()-1; db.commit()
        with pytest.raises(PermissionError): _pa_guards.owned_task(row.id, "owner")


def test_cancellation_revokes_queued_device_commands(sessions):
    import time
    from personal_agent.models import PersonalWorkItem, PersonalDeviceCommand
    from personal_agent.computer.devices import pair, poll
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Control desktop", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.run_id, row.lease_until = "running", "one", time.time()+90
        device = pair(db, "owner", "Computer", "linux", ["click"])
        cmd = PersonalDeviceCommand(id="pending-click", device_id=device["id"], epoch=1, run_id="one",
            work_item_id=row.id, action={"type": "click", "x": 5, "y": 5}, deadline=time.time()+30)
        db.add(cmd); db.commit()
        svc.tasks.cancel(row.id)
        assert poll(db, device["id"], device["token"]) is None
        db.refresh(cmd); assert cmd.status == "revoked"


def test_unread_input_prevents_outdated_final_reply(sessions):
    import time
    from personal_agent.models import PersonalWorkItem
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Original instructions", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.lease_until = "running", "worker", time.time()+90
        db.commit()
        svc.tasks.steer(row.id, "Updated instructions", "two")
        svc.tasks.complete(row.id, "Outdated answer", {"consumed_message_ids": []}, owner="worker")
        # The outdated answer is discarded. The task runs again with the update, without a manual retry.
        assert svc.tasks.list()[0]["status"] == "queued"
        assert [m["content"] for m in svc.conversation.list()] == ["Original instructions", "Updated instructions"]


def test_explicit_attachments_reject_other_owner(sessions):
    from core.db.models import Artifact
    with sessions() as db:
        db.add(Artifact(artifact_id="foreign-file",user_id="other",type="other",mime_type="text/plain",title="foreign.txt",
            filename="foreign.txt",storage_key="private/foreign.txt",size_bytes=5))
        db.commit()
        svc = AgentContext(db, "owner")
        with pytest.raises(LookupError): svc.tasks.submit("Read file", "one", ["foreign-file"])
        assert not svc.tasks.list()




def test_ordinary_chat_cannot_claim_personal_conversation():
    from fastapi import HTTPException
    from core.chat.operations.session_context import _ensure_chat_session
    with pytest.raises(HTTPException) as error:
        _ensure_chat_session(None, "pa_chat_reserved", "owner", "ordinary write")
    assert error.value.status_code == 403


def test_unknown_action_requires_reconciliation_before_retry(sessions):
    from personal_agent.models import PersonalWorkItem
    from personal_agent.tasks.approvals import reconcile
    digest = "a" * 64
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Send a file", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status = "waiting"
        row.checkpoint = {"action_receipts": {digest: {"status": "uncertain"}}, "approved_actions": [digest]}
        db.commit()
        with pytest.raises(ValueError): svc.tasks.retry(row.id)
        reconcile(svc, row.id, digest, "not_executed")
        assert digest not in row.checkpoint["approved_actions"]
        assert svc.tasks.retry(row.id)["status"] == "queued"


@pytest.mark.asyncio
async def test_ordinary_pool_never_adopts_or_kills_personal_computer():
    from types import SimpleNamespace
    from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    # Even a stopped personal computer must bypass the ordinary dead-container cleanup.
    info = SimpleNamespace(status=SimpleNamespace(state="STOPPED"), metadata={"hugagent-owner": "personal-agent"})
    await provider._classify_and_adopt(None, info, {})


def test_approval_preserves_exact_payload_and_steering_revokes_it(sessions):
    from personal_agent.models import PersonalWorkItem
    from personal_agent.tasks.approvals import approve
    digest = "b"*64
    action = {"kind":"email.send", "to":"test@example.org", "body":"Approved exact text"}
    with sessions() as db:
        svc = AgentContext(db,"owner")
        task=svc.tasks.submit("Send mail","one")
        row=db.get(PersonalWorkItem,task["id"])
        row.status="waiting"; row.checkpoint={"pending_approval":{"id":digest,"action":action}}
        db.commit()
        approve(svc,row.id,digest,True)
        assert row.checkpoint["approved_action_payloads"][digest] == action
        svc.tasks.steer(row.id,"Do not send the previous draft","two")
        assert row.checkpoint["approved_action_payloads"] == {}
        assert row.checkpoint["approved_actions"] == []


def test_lifetime_history_starts_recent_and_supports_older_pages(sessions):
    with sessions() as db:
        svc=AgentContext(db,"owner")
        svc.identity.ensure()
        for i in range(105):
            svc.conversation.append(svc.identity.agent(),"user",str(i),"task")
        db.commit()
        recent=svc.conversation.list()
        assert len(recent)==100 and recent[0]["content"]=="5"
        earlier=svc.conversation.list(before_seq=recent[0]["seq"])
        assert [row["content"] for row in earlier]==[str(i) for i in range(5)]


def test_ordinary_search_and_fork_cannot_expose_personal_chat(sessions):
    from fastapi import HTTPException
    from core.db.chat_search import search_sessions
    from core.chat.fork import ChatForkService
    from uuid import uuid4
    with sessions() as db:
        chat=AgentContext(db,"owner").identity.ensure()["chat_id"]
        assert search_sessions(db,"owner","Hugo")[1] == 0
        with pytest.raises(HTTPException): ChatForkService(db).fork(chat,"owner",uuid4())


@pytest.mark.asyncio
async def test_new_approval_after_dispatch_stops_before_model(sessions, monkeypatch):
    import time
    from contextlib import asynccontextmanager
    from personal_agent.tasks import worker as module
    from personal_agent.models import PersonalWorkItem
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    @asynccontextmanager
    async def binding(**kwargs):
        yield object()
    monkeypatch.setattr(module, "durable_run_binding", binding)
    async def model_must_not_run(*args):
        raise AssertionError("Pending approval must stop the model")
    worker = module.PersonalAgentWorker(execute=model_must_not_run)
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Send changed attachment", "one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", worker.identity, "run", time.time()+90
        db.commit()
    async def dispatch(ident, uid):
        with sessions() as db:
            row = db.get(PersonalWorkItem, ident)
            row.checkpoint = {"pending_approval": {"id": "changed-file", "action": {"kind": "email.send"}}}
            db.commit()
    monkeypatch.setattr(worker, "dispatch_approved", dispatch)
    await worker.run(task["id"])
    with sessions() as db:
        row = db.get(PersonalWorkItem, task["id"])
        assert row.status == "waiting"
        assert row.checkpoint["pending_approval"]["id"] == "changed-file"
        assert row.waiting_reason == "等待你批准操作"
