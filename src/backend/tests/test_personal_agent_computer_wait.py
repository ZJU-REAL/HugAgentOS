"""Computer contention defers work without asking its owner to retry."""
import pytest
from tests.test_personal_agent import sessions, personal_agent_on
from personal_agent.context import AgentContext
from personal_agent.models import PersonalWorkItem

@pytest.mark.asyncio
async def test_busy_desktop_resumes_after_release_and_survives_worker_restart(sessions, monkeypatch):
    from core.infra.ephemeral import LocalEphemeralState
    from personal_agent.computer import computer_wait
    state = LocalEphemeralState()
    monkeypatch.setattr(computer_wait, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(computer_wait, "get_ephemeral_state", lambda: state)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        item = svc.tasks.submit("Use desktop", "busy")
        row = db.get(PersonalWorkItem, item["id"])
        row.status = "computer_wait"
        row.checkpoint = {"computer_wait": "pa:desktop:test"}
        db.commit()
        assert svc.tasks.list()[0]["status"] == "queued"
        assert svc.tasks.list()[0]["waiting_reason"] is None
    await state.hold("pa:desktop:test", "human", ttl=45)
    await computer_wait.resume_available()
    with sessions() as db:
        assert db.get(PersonalWorkItem, item["id"]).status == "computer_wait"
    await state.release("pa:desktop:test", "human")
    await computer_wait.resume_available()
    with sessions() as db:
        row = db.get(PersonalWorkItem, item["id"])
        assert row.status == "queued"
        assert "computer_wait" not in row.checkpoint

@pytest.mark.asyncio
@pytest.mark.parametrize("status,receipt,pending", [("waiting", {}, False), ("cancelled", {}, False), ("computer_wait", {"status": "uncertain"}, False), ("computer_wait", {}, True)])
async def test_auto_resume_never_overrides_user_pause_or_unknown_effect(sessions, monkeypatch, status, receipt, pending):
    from core.infra.ephemeral import LocalEphemeralState
    from personal_agent.computer import computer_wait
    monkeypatch.setattr(computer_wait, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(computer_wait, "get_ephemeral_state", LocalEphemeralState)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        item = svc.tasks.submit("Use desktop", "blocked")
        row = db.get(PersonalWorkItem, item["id"])
        row.status = status
        row.checkpoint = {"computer_wait": "pa:desktop:test", "action_receipts": {"a": receipt},
                          **({"pending_approval": {"id": "approval"}} if pending else {})}
        db.commit()
    await computer_wait.resume_available()
    with sessions() as db:
        assert db.get(PersonalWorkItem, item["id"]).status != "queued"


@pytest.mark.asyncio
async def test_action_not_dispatched_or_marked_unknown_while_desktop_busy(sessions, monkeypatch):
    import time, json
    from core.infra import ephemeral
    from personal_agent.computer import computer
    from personal_agent.computer import computer_wait
    from personal_agent.tools import tools
    from personal_agent.tasks import approvals
    from personal_agent.tasks.execution import current_execution
    current_execution.set(None)
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(computer_wait, "get_ephemeral_state", lambda: state)
    for module in (computer_wait, tools, approvals): monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(computer, "LOCK_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(computer, "LOCK_POLL_SECONDS", 0)
    calls = []
    async def driver(*args):
        calls.append(args)
        return {"status": "input_executed"}
    monkeypatch.setattr(computer, "cloud_driver", driver)
    class Toolkit:
        def __init__(self): self.functions = {}
        def append(self, tool):
            fn = tool._func
            self.functions[fn.__name__] = fn
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.configure({"approval_mode": "full"})
        item = svc.tasks.submit("Use desktop", "tool-busy")
        row = db.get(PersonalWorkItem, item["id"])
        row.status, row.owner, row.lease_until = "running", "worker", time.time()+90
        key = "pa:desktop:"+row.agent_id
        db.commit()
    kit = Toolkit()
    computer.register_computer_tools(kit, item["id"], "owner")
    await state.hold(key, "human", ttl=45)
    result = json.loads((await kit.functions["pa_computer_action"]({"type": "key", "key": "enter", "observation_id": "screen"})).content[0].text)
    assert result["automatic_retry"] and calls == []
    with sessions() as db:
        row = db.get(PersonalWorkItem, item["id"])
        assert not row.unconfirmed()
        assert computer_wait.park(row)
        db.commit()
    await state.release(key, "human")
    await computer_wait.resume_available()
    with sessions() as db:
        row = db.get(PersonalWorkItem, item["id"])
        assert row.status == "queued"
        row.status, row.owner, row.lease_until = "running", "worker", time.time()+90
        db.commit()
    result = json.loads((await kit.functions["pa_computer_action"]({"type": "key", "key": "enter", "observation_id": "screen"})).content[0].text)
    assert result["status"] == "input_executed" and len(calls) == 1
    assert await state.get(key) is None

@pytest.mark.asyncio
async def test_legacy_busy_wait_requeues_without_manual_retry(sessions, monkeypatch):
    from core.infra.ephemeral import LocalEphemeralState
    from personal_agent.computer import computer_wait
    monkeypatch.setattr(computer_wait, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(computer_wait, "get_ephemeral_state", LocalEphemeralState)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        item = svc.tasks.submit("Old desktop wait", "legacy-busy")
        row = db.get(PersonalWorkItem, item["id"])
        row.status, row.waiting_reason = "waiting", computer_wait.BUSY_REASON
        row.checkpoint = {"task_wait_reason": computer_wait.BUSY_REASON, "action_receipts": {"a": {"status": "input_executed"}}}
        db.commit()
    await computer_wait.resume_available()
    with sessions() as db:
        row = db.get(PersonalWorkItem, item["id"])
        assert row.status == "queued"
        assert "task_wait_reason" not in row.checkpoint


@pytest.mark.asyncio
async def test_worker_preserves_unknown_tool_outcome_despite_desktop_wait(sessions, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from personal_agent.tasks import worker as module
    from core.runs.tool_effects.errors import ToolOutcomeUnknown
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    @asynccontextmanager
    async def binding(**kwargs):
        yield SimpleNamespace(run_id=kwargs["binding_run_id"], owner="test")
    monkeypatch.setattr(module, "durable_run_binding", binding)
    async def execute(ident, binding):
        with sessions() as db:
            row = db.get(PersonalWorkItem, ident)
            row.checkpoint = {"computer_wait": "pa:desktop:test"}
            db.commit()
        raise ToolOutcomeUnknown("External write result unknown")
    worker = module.PersonalAgentWorker(execute=execute)
    monkeypatch.setattr(worker, "dispatch_approved", lambda *args: asyncio.sleep(0))
    with sessions() as db:
        item = AgentContext(db, "owner").tasks.submit("Use tools", "unknown")
    assert worker.claim() == item["id"]
    await worker.run(item["id"])
    with sessions() as db:
        row = db.get(PersonalWorkItem, item["id"])
        assert row.status == "waiting"
        assert "未知" in row.waiting_reason
