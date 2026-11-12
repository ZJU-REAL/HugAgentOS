"""What the agent learns about a HugAgentOS session operation: the receipt's status and reason,
for the web and for the user's computers, and when the same write may be asked again."""

import asyncio
import json
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db.engine import Base
from core.db.models import UserShadow
from hugagent_cli import contract
from personal_agent.computer import devices
from personal_agent.context import AgentContext
from personal_agent.models import PersonalDevice, PersonalSessionOperation as Operation, PersonalWorkItem
from personal_agent.sessions import session_cloud, session_manager, session_queue, session_tool
from personal_agent.tasks import approvals

OLD_UNKNOWN = {"outcome_unknown": True, "error": "local_failure; check the result before retry"}


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (approvals, session_tool, devices):
        monkeypatch.setattr(module, "SessionLocal", make)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", make)
    monkeypatch.setattr("core.db.engine.SessionLocal", make)
    # Every write runs without a card here; the approval gate has its own tests.
    monkeypatch.setattr(approvals, "preapproved", lambda mode, action: True)
    monkeypatch.setattr(session_manager, "POLL_SEC", 0.05)
    with make() as db:
        db.add(UserShadow(user_id="owner", user_center_id="owner", username="owner"))
        db.commit()
    yield make
    engine.dispose()


def running_task(make):
    with make() as db:
        task_id = AgentContext(db, "owner").tasks.submit("Manage sessions", "task")["id"]
        row = db.get(PersonalWorkItem, task_id)
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 300
        db.commit()
    return task_id


def linked(make, contract_name=None):
    with make() as db:
        device = devices.pair(db, "owner", "Mac", "macos", ["hugagent.sessions"])
        devices.heartbeat(db, device["id"], device["token"], False, True,
                          contract=contract_version() if contract_name is None else contract_name)
    return device


def contract_version():
    return contract.contract_version()


async def desktop(make, device, receipts, *, take=True):
    """A desktop that answers each operation it claims with the next receipt (None: never answers)."""
    receipts = list(receipts)
    while receipts:
        await asyncio.sleep(0.02)
        with make() as db:
            db.get(PersonalDevice, device["id"]).last_seen = time.time()
            db.commit()
            if not take:
                continue
            job = session_queue.poll(db, device["id"], device["token"])
            if job is None:
                continue
            receipt = receipts.pop(0)
            if receipt is not None:
                session_queue.acknowledge(db, device["id"], device["token"], job["id"], receipt)


async def tool_call(make, device, receipts, operation, args, task_id=None, take=True):
    tool = session_tool.build("owner", task_id)
    answer = asyncio.create_task(desktop(make, device, receipts, take=take))
    chunk = await tool(operation, args, target="device:" + device["id"])
    answer.cancel()
    return json.loads(chunk.content[0].text)


def short_deadline(monkeypatch):
    monkeypatch.setattr(session_queue, "DEADLINE_SECONDS", 0.6)
    monkeypatch.setattr(session_queue, "RECEIPT_GRACE", 0.2)


# ── what one finished operation reads as ─────────────────────────────────────────────

def job(status="completed", operation="chat.send", result=None, claimed=True, target="device:d"):
    return {"id": "op", "target": target, "operation": operation, "status": status, "result": result,
            "deadline": 0, "claimed": claimed}


def test_report_never_calls_a_failure_completed():
    assert session_tool.report(job(result={"run_id": "r"})) == {"status": "completed", "result": {"run_id": "r"}}
    refused = session_tool.report(job(result=contract.not_executed("http_409", http_status=409, detail="chat_busy", retryable=False)))
    assert refused["status"] == "not_executed" and refused["http_status"] == 409 and refused["detail"] == "chat_busy"
    assert refused["retryable"] is False and refused["message"].startswith("Did not run; HTTP 409: chat_busy")
    assert "safe to retry after fixing the cause" in refused["message"]
    unknown = session_tool.report(job(result=contract.outcome_unknown("timed_out", exception="ReadTimeout")))
    assert unknown["status"] == "outcome_unknown" and "check" in unknown["message"] and "may have taken effect" in unknown["message"].lower()
    plain = session_tool.report(job(result={"error": "boom"}))
    assert plain["status"] == "failed" and "boom" in plain["message"]


def test_an_old_desktops_failed_read_is_retryable_not_unknown():
    report = session_tool.report(job(operation="chat.list", result=OLD_UNKNOWN))
    assert report["status"] == "not_executed" and report["retryable"] is True
    assert report["error"] == "local_failure" and "safe to retry" in report["message"]
    assert "check the result" not in json.dumps(report)
    # The same receipt on a write stays uncertain until someone checks.
    assert session_tool.report(job(result=OLD_UNKNOWN))["status"] == "outcome_unknown"


@pytest.mark.parametrize("state,operation,claimed,status,words", [
    ("expired", "chat.send", False, "not_executed", "did not pick this up"),
    ("outcome_unknown", "chat.send", True, "outcome_unknown", "No answer"),
    ("outcome_unknown", "chat.list", True, "not_executed", "Safe to retry"),
    ("revoked", "chat.send", False, "not_executed", "did not run"),
    ("revoked", "chat.send", True, "outcome_unknown", "may have taken effect"),
    ("revoked", "chat.list", True, "not_executed", "did not run"),
    (session_queue.RECONCILED, "chat.send", True, "not_executed", "did not take effect"),
])
def test_queue_endings_say_what_happened(state, operation, claimed, status, words):
    report = session_tool.report(job(state, operation, claimed=claimed))
    assert report["status"] == status and words in report["message"]


# ── through the tool, with a desktop answering ───────────────────────────────────────

async def test_a_device_read_that_failed_on_an_old_desktop_reads_as_not_executed(factory):
    device = linked(factory)
    result = await tool_call(factory, device, [OLD_UNKNOWN], "chat.list", {})
    assert result["status"] == "not_executed" and result["retryable"] is True and "desktop_note" not in result
    ok = await tool_call(factory, device, [{"items": []}], "chat.list", {})
    assert ok == {"status": "completed", "result": {"items": []}}


async def test_a_refused_device_write_can_be_asked_again(factory):
    device, task_id = linked(factory), running_task(factory)
    args = {"chat_id": "c1", "message": "go"}
    busy = contract.not_executed("http_409", http_status=409, detail="chat_busy", retryable=False)
    first = await tool_call(factory, device, [busy], "chat.send", args, task_id)
    assert first["status"] == "not_executed" and first["detail"] == "chat_busy" and first["http_status"] == 409
    second = await tool_call(factory, device, [{"run_id": "run_1"}], "chat.send", args, task_id)
    assert second == {"status": "completed", "result": {"run_id": "run_1"}}
    with factory() as db:
        assert db.scalar(select(Operation).where(Operation.operation == "chat.send")).result == {"run_id": "run_1"}
        assert AgentContext(db, "owner").tasks.get(task_id).checkpoint["action_receipts"]
    # A completed write keeps its receipt: asking again does not send it again.
    again = await tool_call(factory, device, [], "chat.send", args, task_id)
    assert again == {"status": "completed", "result": {"run_id": "run_1"}}


async def test_an_uncertain_write_waits_for_the_user_then_runs_again(factory):
    device, task_id = linked(factory), running_task(factory)
    args = {"chat_id": "c1", "message": "go"}
    unknown = contract.outcome_unknown("timed_out", exception="ReadTimeout")
    first = await tool_call(factory, device, [unknown], "chat.send", args, task_id)
    assert first["status"] == "outcome_unknown" and first["error"] == "timed_out" and first["exception"] == "ReadTimeout"
    # Never repeated blindly: the same call returns the same operation.
    again = await tool_call(factory, device, [{"run_id": "x"}], "chat.send", args, task_id)
    assert again["status"] == "outcome_unknown"
    with factory() as db:
        assert db.query(Operation).count() == 1
        row = db.get(PersonalWorkItem, task_id)
        row.status = "waiting"
        db.commit()
        svc = AgentContext(db, "owner")
        [uncertain] = svc.tasks.list()[0]["uncertain_actions"]
        approvals.reconcile(svc, task_id, uncertain, "not_executed")
        row = db.get(PersonalWorkItem, task_id)
        row.status, row.lease_until = "running", time.time() + 300
        db.commit()
    third = await tool_call(factory, device, [{"run_id": "run_2"}], "chat.send", args, task_id)
    assert third == {"status": "completed", "result": {"run_id": "run_2"}}


async def test_a_write_no_computer_took_did_not_run_and_may_be_asked_again(factory, monkeypatch):
    short_deadline(monkeypatch)
    device, task_id = linked(factory), running_task(factory)
    args = {"title": "T"}
    expired = await tool_call(factory, device, [None], "chat.create", args, task_id, take=False)
    assert expired["status"] == "not_executed" and "did not pick this up" in expired["message"]
    with factory() as db:
        assert AgentContext(db, "owner").tasks.list()[0]["uncertain_actions"] == []
    created = await tool_call(factory, device, [{"chat_id": "c9"}], "chat.create", args, task_id)
    assert created["status"] == "completed"


async def test_a_write_taken_but_never_answered_is_uncertain(factory, monkeypatch):
    short_deadline(monkeypatch)
    device, task_id = linked(factory), running_task(factory)
    silent = await tool_call(factory, device, [None], "chat.create", {"title": "T"}, task_id)
    assert silent["status"] == "outcome_unknown" and "No answer" in silent["message"]
    with factory() as db:
        row = db.get(PersonalWorkItem, task_id)
        row.status = "waiting"
        db.commit()
        assert len(AgentContext(db, "owner").tasks.list()[0]["uncertain_actions"]) == 1


async def test_a_refusal_before_queueing_is_not_executed(factory):
    device = linked(factory)
    with factory() as db:
        db.get(PersonalDevice, device["id"]).last_seen = time.time() - 600
        db.commit()
    result = json.loads((await session_tool.build("owner")("chat.list", {}, target="device:" + device["id"])).content[0].text)
    assert result["status"] == "not_executed" and "offline" in result["message"]


async def test_an_out_of_date_desktop_is_named(factory):
    device = linked(factory, contract_name="0123456789abcdef")
    result = await tool_call(factory, device, [{"items": []}], "chat.list", {})
    assert result["status"] == "completed" and "may need an update" in result["desktop_note"]
    [listed] = devices.list_devices("owner")
    assert listed["contract"] == "0123456789abcdef" and listed["contract_matches"] is False and "update" in listed["note"]
    with factory() as db:
        devices.heartbeat(db, device["id"], device["token"], False, True, contract=contract_version(), app_version="0.6.0")
    [listed] = devices.list_devices("owner")
    assert listed["contract_matches"] is True and listed["app_version"] == "0.6.0" and "note" not in listed


# ── the web ─────────────────────────────────────────────────────────────────────────

async def test_web_failures_carry_their_reason(factory, monkeypatch):
    task_id = running_task(factory)

    async def execute(user, operation, args, origin=None):
        if operation == "chat.get":
            raise HTTPException(404, "Chat not found")
        raise RuntimeError("stream broke")

    monkeypatch.setattr(session_cloud, "execute", execute)
    tool = session_tool.build("owner", task_id)
    missing = json.loads((await tool("chat.get", {"chat_id": "c1"}, target="web")).content[0].text)
    assert missing["status"] == "not_executed" and missing["http_status"] == 404 and missing["detail"] == "Chat not found"
    broke = json.loads((await tool("chat.send", {"chat_id": "c1", "message": "m"})).content[0].text)
    assert broke["status"] == "outcome_unknown" and broke["exception"] == "RuntimeError" and broke["detail"] == "stream broke"
    # "web" and "cloud" are one place, so one write keeps one key.
    assert session_manager.normalize_target("web") == "cloud"
    with factory() as db:
        assert {row.target for row in db.scalars(select(Operation))} == {"cloud"}


def test_the_tool_calls_the_web_target_web():
    doc = session_tool.build("owner", "t").__doc__
    assert "target is web" in doc and "not your own cloud computer" in doc


# ── an approved action run before the model ──────────────────────────────────────────

async def test_dispatch_approved_marks_only_a_real_unknown_uncertain(factory, monkeypatch):
    short_deadline(monkeypatch)
    device, task_id = linked(factory), running_task(factory)
    target = "device:" + device["id"]
    action = {"kind": "hugagent.session", "target": target, "operation": "chat.create", "args": {"title": "T"},
              "request_key": session_tool.request_key(task_id, target, "chat.create", {"title": "T"})}
    job = await session_manager.dispatch_approved(task_id, "owner", action)  # nobody takes it
    assert job["status"] == "expired" and "outcome_unknown" not in job
    with factory() as db:
        receipt = db.get(PersonalWorkItem, task_id).checkpoint["action_receipts"][approvals.fingerprint(action)]
        assert receipt["status"] == "expired" and not receipt.get("outcome_unknown")
    answer = asyncio.create_task(desktop(factory, device, [contract.outcome_unknown("timed_out")]))
    job = await session_manager.dispatch_approved(task_id, "owner", action)
    answer.cancel()
    assert job["outcome_unknown"] is True
    with factory() as db:
        assert db.get(PersonalWorkItem, task_id).checkpoint["action_receipts"][approvals.fingerprint(action)]["outcome_unknown"]


# ── receipts at the deadline ─────────────────────────────────────────────────────────

def test_a_receipt_just_past_the_deadline_still_lands(factory, monkeypatch):
    real_time = time.time
    device = linked(factory)
    with factory() as db:
        job = session_queue.submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "A"}, "k")
        session_queue.poll(db, device["id"], device["token"])
        row = db.get(Operation, job["id"])
        late = row.deadline + session_queue.RECEIPT_GRACE / 2
        monkeypatch.setattr(session_queue.time, "time", lambda: late)
        assert session_queue.check(db, device["id"], device["token"], job["id"]) == {"permitted": True}
        assert session_queue.view(db, "owner", job["id"])["status"] == "sent"
        session_queue.acknowledge(db, device["id"], device["token"], job["id"], contract.outcome_unknown("timed_out"))
        monkeypatch.setattr(session_queue.time, "time", real_time)
        job2 = session_queue.submit(db, "owner", "device:" + device["id"], "chat.create", {"title": "B"}, "k2")
        session_queue.poll(db, device["id"], device["token"])
        too_late = db.get(Operation, job2["id"]).deadline + session_queue.RECEIPT_GRACE + 1
        monkeypatch.setattr(session_queue.time, "time", lambda: too_late)
        with pytest.raises(ValueError):
            session_queue.acknowledge(db, device["id"], device["token"], job2["id"], {"chat_id": "c"})


def test_unknown_capabilities_are_ignored_not_refused(factory):
    with factory() as db:
        device = devices.pair(db, "owner", "Mac", "macos", ["hugagent.sessions", "teleport"])
        assert db.get(PersonalDevice, device["id"]).capabilities == ["hugagent.sessions"]
        beat = devices.heartbeat(db, device["id"], device["token"], False, True, ["hugagent.sessions", "teleport"] * 30)
        assert beat["sessions_enabled"] is True
