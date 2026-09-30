"""Exercise real lifecycle/state orchestration with only the sandbox transport replaced."""

import asyncio
import json
from pathlib import PurePosixPath

import pytest
from core.db.engine import Base
from core.services.job_service import JobService
from orchestration import job_runtime as runtime
from orchestration.jobs import files, notifications, owner, process, state, supervisor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class Sandbox:
    name = "test"

    def __init__(self):
        self.files = {}
        self.commands = {}
        self.handshake = True
        self.exit_immediately = False
        self.upload_gate = None
        self.transport_error = False

    async def put_file(self, session, path, data, **kwargs):
        if self.upload_gate:
            await self.upload_gate.wait()
        self.files[path] = data

    async def get_file(self, session, path, **kwargs):
        return self.files[path]

    async def start_process(self, req, **kwargs):
        config_path = next(p for p in reversed(self.files) if p.endswith("execution.json"))
        config = json.loads(self.files[config_path])
        jid = config["JOB_ID"]
        command = {"status": "running", "session_id": jid}
        self.commands[jid] = command
        if self.handshake:
            with state.SessionLocal() as db:
                JobService(db).mark_running(jid)
        if self.exit_immediately:
            command.update(status="exited", exit_code=127)
        return dict(command)

    async def write_stdin(self, sid, *, chars="", **kwargs):
        command = self.commands[sid]
        if chars:
            command.update(status="exited", exit_code=130)
        if self.transport_error:
            return {"status": "exited", "error": "connection lost"}
        return dict(command)

    def complete(self, jid):
        row = state.snapshot(jid)
        execution = row["meta"]["execution"]
        self.files[execution["directory"] + "/lifecycle.final"] = json.dumps(
            {"attempt_id": execution["attempt_id"], "status": "completed"}
        ).encode()
        self.commands[jid].update(status="exited", exit_code=0)


@pytest.fixture
def sandbox(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    for module in (runtime, state):
        monkeypatch.setattr(module, "SessionLocal", session)
    monkeypatch.setattr(owner, "_loop", None)
    monkeypatch.setattr(supervisor, "_entries", {})
    box = Sandbox()
    monkeypatch.setattr(process, "get_sandbox_provider", lambda: box)
    monkeypatch.setenv("JOB_CALLBACK_URL", "http://test.invalid")

    async def no_wake(*args, **kwargs):
        pass

    async def logs(*args, **kwargs):
        return 0, "python: startup failed", ""

    monkeypatch.setattr(files, "_sbx_bash", logs)
    monkeypatch.setattr(supervisor, "_maybe_wake", no_wake)
    monkeypatch.setattr(notifications, "_maybe_wake", no_wake)
    yield box
    engine.dispose()


async def start(**kwargs):
    owner.bind()
    return await runtime.start_job(
        user_id="user",
        chat_id=None,
        name="test",
        script_path="test.py",
        script_text="pass",
        session_id="session",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_worker_loop_exit_does_not_cancel_monitor(sandbox):
    owner.bind()

    async def worker():
        return await runtime.start_job(
            user_id="user",
            chat_id=None,
            name="thread",
            script_path="test.py",
            script_text="pass",
            session_id="session",
        )

    jid = await asyncio.to_thread(lambda: asyncio.run(worker()))
    assert state.snapshot(jid)["status"] == "running"
    assert not supervisor._entries[jid].task.done()
    sandbox.complete(jid)
    assert (await runtime.run_and_wait(jid))["status"] == "completed"


@pytest.mark.asyncio
async def test_start_failure_is_reported_and_process_exit_confirmed(sandbox):
    sandbox.handshake = False
    sandbox.exit_immediately = True
    with pytest.raises(RuntimeError, match="未留下完成回执"):
        await start()
    row = state.snapshot(next(iter(sandbox.commands)))
    assert row["status"] == "failed"
    assert row["meta"]["execution"]["phase"] == "exited"


@pytest.mark.asyncio
async def test_missing_handshake_stops_exact_command(sandbox, monkeypatch):
    sandbox.handshake = False
    monkeypatch.setattr(supervisor, "STARTUP_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="启动超时"):
        await start()
    row = state.snapshot(next(iter(sandbox.commands)))
    assert row["status"] == "failed"
    assert row["meta"]["execution"]["phase"] == "exited"


@pytest.mark.asyncio
async def test_cancel_one_job_keeps_other_job_alive_and_can_resume(sandbox):
    first = await start()
    second = await start()
    assert await runtime.cancel_job(first, user_id="user")
    await runtime.run_and_wait(first)
    assert sandbox.commands[second]["status"] == "running"
    assert state.snapshot(first)["status"] == "cancelled"
    result = await runtime.resume_job(first, user_id="user")
    assert result["ok"]
    sandbox.complete(first)
    sandbox.complete(second)
    assert (await runtime.run_and_wait(first))["status"] == "completed"
    assert (await runtime.run_and_wait(second))["status"] == "completed"


@pytest.mark.asyncio
async def test_transport_failure_does_not_allow_duplicate_resume(sandbox):
    jid = await start()
    sandbox.transport_error = True
    assert (await runtime.run_and_wait(jid))["status"] == "interrupted"
    result = await runtime.resume_job(jid, user_id="user")
    assert not result["ok"]
    assert "禁止" in result["error"]


@pytest.mark.asyncio
async def test_cancel_during_upload_never_launches(sandbox):
    owner.bind()
    sandbox.upload_gate = asyncio.Event()
    launching = asyncio.create_task(start())
    for _ in range(50):
        if runtime._launches:
            break
        await asyncio.sleep(0.01)
    jid = next(iter(runtime._launches))
    cancel = asyncio.create_task(runtime.cancel_job(jid, user_id="user"))
    await asyncio.sleep(0.05)
    sandbox.upload_gate.set()
    assert await cancel
    result = await asyncio.gather(launching, return_exceptions=True)
    assert isinstance(result[0], RuntimeError)
    assert not sandbox.commands
    assert state.snapshot(jid)["status"] == "cancelled"
    assert state.snapshot(jid)["meta"]["execution"]["phase"] == "not_started"


@pytest.mark.asyncio
async def test_caller_cancellation_keeps_launch_owned(sandbox):
    owner.bind()
    sandbox.upload_gate = asyncio.Event()
    caller = asyncio.create_task(start())
    for _ in range(50):
        if runtime._launches:
            break
        await asyncio.sleep(0.01)
    jid = next(iter(runtime._launches))
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    sandbox.upload_gate.set()
    for _ in range(50):
        if jid in sandbox.commands:
            break
        await asyncio.sleep(0.01)
    sandbox.complete(jid)
    assert (await runtime.run_and_wait(jid))["status"] == "completed"


@pytest.mark.asyncio
async def test_wall_clock_budget_stops_process(sandbox):
    jid = await start(budget={"max_seconds": 1})
    result = await runtime.run_and_wait(jid)
    assert result["status"] == "failed"
    assert "墙钟" in result["error"]
    assert sandbox.commands[jid]["status"] == "exited"


@pytest.mark.asyncio
async def test_old_monitor_cancellation_cannot_kill_new_owner(sandbox):
    jid = await start()
    row = state.snapshot(jid)
    attempt = row["meta"]["execution"]["attempt_id"]
    task = supervisor._entries[jid].task
    state.save_execution(jid, attempt, owner="another-worker")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sandbox.commands[jid]["status"] == "running"
    assert state.snapshot(jid)["status"] == "running"


@pytest.mark.asyncio
async def test_expired_lease_is_reattached_without_relaunch(sandbox, monkeypatch):
    from datetime import timedelta

    jid = await start()
    entry = supervisor._entries[jid]
    state.save_execution(
        jid,
        entry.execution.attempt_id,
        owner="dead-worker",
        lease_until=(state.now() - timedelta(seconds=1)).isoformat(),
    )
    entry.task.cancel()
    await asyncio.gather(entry.task, return_exceptions=True)

    async def recover(row):
        assert row["job_id"] == jid
        return entry.execution

    monkeypatch.setattr(process, "recover", recover)
    assert await runtime.reap_orphan_jobs() == 1
    assert len(sandbox.commands) == 1
    sandbox.complete(jid)
    assert (await runtime.run_and_wait(jid))["status"] == "completed"


@pytest.mark.asyncio
async def test_missing_cancel_retains_not_found_contract(sandbox):
    owner.bind()
    assert await runtime.cancel_job("missing", user_id="user") is False


@pytest.mark.asyncio
async def test_slow_progress_notification_does_not_block_budget(sandbox, monkeypatch):
    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(supervisor, "_maybe_wake_progress", slow)
    jid = await start(budget={"max_seconds": 2}, start_params={"progress_wake_sec": 0.001})
    result = await asyncio.wait_for(runtime.run_and_wait(jid), 5)
    assert entered.is_set()
    assert result["status"] == "failed"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_cancel_route_reports_uncertain_stop_as_conflict(monkeypatch):
    from types import SimpleNamespace

    from api.routes.v1 import jobs
    from fastapi import HTTPException

    async def uncertain(*args, **kwargs):
        raise RuntimeError("已撤销回调权限，但无法确认停止")

    monkeypatch.setattr(runtime, "cancel_job", uncertain)
    with pytest.raises(HTTPException) as err:
        await jobs.cancel_job("job", user=SimpleNamespace(user_id="user"))
    assert err.value.status_code == 409
    assert "无法确认" in err.value.detail


@pytest.mark.asyncio
async def test_cancel_lost_ownership_never_confirms_exit(sandbox, monkeypatch):
    jid = await start()
    entry = supervisor._entries[jid]
    supervisor._entries.pop(jid)

    async def recover(row):
        state.save_execution(jid, entry.execution.attempt_id, owner="other-canceller")
        return entry.execution

    monkeypatch.setattr(process, "recover", recover)
    with pytest.raises(RuntimeError, match="无法确认"):
        await runtime.cancel_job(jid, user_id="user")
    assert sandbox.commands[jid]["status"] == "running"
    assert state.snapshot(jid)["meta"]["execution"]["phase"] != "exited"
    assert not (await runtime.resume_job(jid, user_id="user"))["ok"]
    entry.task.cancel()
    await asyncio.gather(entry.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_local_cancel_timeout_has_structured_conflict(sandbox, monkeypatch):
    from types import SimpleNamespace

    from api.routes.v1 import jobs
    from fastapi import HTTPException

    jid = await start()

    async def timeout():
        raise asyncio.TimeoutError()

    monkeypatch.setattr(supervisor._entries[jid].execution, "stop", timeout)
    with pytest.raises(HTTPException) as err:
        await jobs.cancel_job(jid, user=SimpleNamespace(user_id="user"))
    assert err.value.status_code == 409
    sandbox.complete(jid)
    await runtime.run_and_wait(jid)
