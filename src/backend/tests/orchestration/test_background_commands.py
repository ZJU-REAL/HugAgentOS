"""Background command ownership, handoff and completion delivery."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.chat.runtime.worker as runtime_worker
import core.db.engine as db_engine
import core.sandbox.commands.events as commands_events
import core.sandbox.commands.notifications as commands_notifications
import core.sandbox.commands.runtime as commands_runtime
import pytest
from core.db import models
from core.db.engine import Base
from core.sandbox.commands.runtime import CommandRuntime
from core.sandbox.commands import store
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.orchestration.command_test_support import source_run


@pytest.fixture
def command_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "commands.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store, "SessionLocal", factory)
    import core.sandbox.commands.events as events
    isolated_events = events.MemoryCommandEvents()
    monkeypatch.setattr(commands_events, "get_command_events", lambda: isolated_events)
    with factory() as db:
        db.add(models.UserShadow(user_id="u", username="test"))
        db.flush()
        db.add(models.ChatSession(chat_id="c", user_id="u", title="commands"))
        db.commit()
    yield factory
    engine.dispose()

def request():
    from core.sandbox.protocol import ProcessRequest
    return ProcessRequest(script_content="sleep 1", script_name="test.sh", language="bash",
                          session_id="c", user_id="u", timeout=30)

async def test_handoff_does_not_restart_and_readers_are_independent(command_db):
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p","stdout":"first","stderr":""}),
        write_stdin=AsyncMock(return_value={"status":"exited","session_id":None,"stdout":"second","stderr":"","exit_code":0}))
    runtime = CommandRuntime()
    result = await runtime.start(provider, request(), chat_id="c", source_run_id=None,
                                 background=True, yield_time_ms=250)
    await runtime.wait(result["task_id"], "u", "c", 2000)
    assert provider.start_process.await_count == 1
    first = store.read(result["task_id"], "u", "c", consumer="model")
    assert result["stdout"] == "first"
    assert first["stdout"] == "second"
    assert store.read(result["task_id"], "u", "c", consumer="model")["stdout"] == ""
    assert store.read(result["task_id"], "u", "c", consumer="ui")["stdout"] == "firstsecond"
    assert store.snapshot(result["task_id"])["notice_status"] == "consumed"
    await runtime.shutdown()

async def test_cross_owner_cannot_read_or_cancel(command_db):
    row = store.create(request(), "c", None)
    with pytest.raises(PermissionError):
        store.read(row["task_id"], "other", "c")
    runtime = CommandRuntime()
    with pytest.raises(PermissionError):
        await runtime.cancel(row["task_id"], "other", "c")
    assert store.snapshot(row["task_id"])["status"] == "starting"

def test_terminal_notification_is_atomic_with_result(command_db):
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"session_id":"p","status":"running"}, None)
    store.collect(row["task_id"], "owner",
                  {"status":"exited","stdout":"done","stderr":"","exit_code":0})
    done = store.snapshot(row["task_id"])
    assert done["status"] == "completed"
    assert done["notice_status"] == "pending"
    assert done["stdout"] == "done"

@pytest.fixture
async def http_provider(tmp_path, monkeypatch):
    import httpx
    from core.sandbox.providers.script_runner.provider import ScriptRunnerProvider
    from core.sandbox.runner import server
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(server, "_AUTH_TOKEN", "")
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    provider = ScriptRunnerProvider()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://runner")
    async def body(req):
        return {"capability_view_key": None, **{k: getattr(req, k) for k in (
            "script_content", "script_name", "language", "session_id", "user_id", "timeout", "cwd")}}
    async def transport(path, body, wait_ms=0):
        response = await client.post(path, json=body)
        response.raise_for_status()
        return response.json()
    async def touch_session(session_id):
        # Lifecycle traffic belongs to this ASGI runner too, never the configured host.
        response = await client.post("/sessions/touch", json={"session_id": session_id})
        response.raise_for_status()
    monkeypatch.setattr(provider, "_execution_body", body)
    monkeypatch.setattr(provider, "_process_request", transport)
    monkeypatch.setattr(provider, "touch_session", touch_session)
    yield provider
    await server.process_sessions.close_all()
    await client.aclose()

async def test_real_process_background_concurrency_and_cancel(command_db, http_provider, monkeypatch, tmp_path):
    import core.sandbox.commands.runtime as module
    from core.llm.tools.sandbox_tool import register_bash
    runtime = CommandRuntime()
    monkeypatch.setattr(commands_runtime, "runtime", runtime)
    from core.llm.ports import process_state
    monkeypatch.setattr(process_state(), "commands", runtime)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: http_provider)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    class Tools:
        def __init__(self): self.functions = {}
        def append(self, tool):
            fn = tool._func
            _ = {"func_description": tool.description, "permission": getattr(tool, "dispatch_spec", None)}
            self.functions[fn.__name__] = fn
    tools = Tools()
    register_bash(tools, loader=None, chat_id="c", user_id="u")
    def data(response): return json.loads(response.content[0].text)
    marker = tmp_path / "launches"
    command = f"printf x >> '{marker}'; printf first; sleep 1; printf second"
    first = data(await tools.functions["Bash"](command, run_in_background=True))
    assert first["status"] == "running"
    independent = data(await tools.functions["Bash"]("printf independent"))
    assert independent["stdout"] == "independent"
    done = data(await tools.functions["write_stdin"](first["session_id"], yield_time_ms=3000))
    assert done["exit_code"] == 0
    assert first["stdout"] + done["stdout"] == "firstsecond"
    assert marker.read_text() == "x"
    assert store.read(first["task_id"], "u", "c", consumer="ui")["stdout"] == "firstsecond"
    long = data(await tools.functions["Bash"]("printf running; sleep 30", run_in_background=True))
    stopped = data(await tools.functions["write_stdin"](long["session_id"], chars="\x03"))
    assert stopped["task_status"] == "cancelled"
    assert store.snapshot(long["task_id"])["notice_status"] == "consumed"
    await runtime.shutdown()

async def test_launch_cannot_be_stolen_by_reconciler(command_db):
    started = asyncio.Event()
    release = asyncio.Event()
    async def launch(req, **kw):
        started.set()
        await release.wait()
        return {"status":"exited","stdout":"once","stderr":"","exit_code":0}
    provider = SimpleNamespace(name="test", start_process=launch)
    first, other = CommandRuntime(), CommandRuntime()
    launching = asyncio.create_task(first.start(provider, request(), chat_id="c",
                                                source_run_id=None))
    await started.wait()
    await other.recover(provider)
    release.set()
    result = await launching
    assert result["stdout"] == "once"

async def test_adoption_is_atomic_and_starts_one_collector(command_db):
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        write_stdin=AsyncMock(return_value={"status":"running","session_id":"old",
                                           "stdout":"one","stderr":""}))
    runtime = CommandRuntime()
    a, b = await asyncio.gather(*[runtime.adopt(provider, "old", user_id="u", session_id="c",
        chat_id="c", source_run_id=None) for _ in range(2)])
    assert a["task_id"] == b["task_id"]
    assert len(runtime.providers) == 1
    provider.write_stdin.return_value = {"status":"exited","exit_code":0,"stdout":"","stderr":""}
    await runtime.wait(a["task_id"], "u", "c", 2000)
    await runtime.shutdown()

async def test_soft_delete_stops_real_background_process(command_db, http_provider):
    from datetime import datetime, timezone
    runtime = CommandRuntime()
    result = await runtime.start(http_provider, request(), chat_id="c", source_run_id=None,
                                 background=True)
    with command_db() as db:
        db.get(models.ChatSession, "c").deleted_at = datetime.now(timezone.utc)
        db.commit()
    await runtime.wait(result["task_id"], "u", "c", 3000)
    assert store.snapshot(result["task_id"])["status"] == "cancelled"
    assert store.snapshot(result["task_id"])["notice_status"] == "consumed"
    await runtime.shutdown()

async def test_busy_react_delivery_and_final_output_race(command_db):
    run_id = source_run(command_db)
    from agentscope.agent import Agent
    from agentscope.message import Msg, TextBlock
    from agentscope.model import ChatModelBase, ChatResponse
    from core.sandbox.commands.completion_hook import command_completion_hooks
    from tests.orchestration.hook_support import make_adapter
    from core.llm.runtime.state import AgentRuntimeState
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    seen = []
    class Model(ChatModelBase):
        def __init__(self):
            super().__init__(None, "deterministic", self.Parameters(), stream=False, max_retries=0)
            from agentscope.formatter import OpenAIChatFormatter
            self.formatter = OpenAIChatFormatter()
        async def count_tokens(self, **kwargs): return 10
        async def _call_api(self, model_name, messages, **kwargs):
            seen.append([str(m.content) for m in messages])
            if len(seen) == 1:
                store.collect(row["task_id"], "owner",
                    {"status":"exited","exit_code":0,"stdout":"ready","stderr":""})
            return ChatResponse(content=[TextBlock(type="text", text="step")], is_last=True)
    agent = Agent(name="test", system_prompt="test", model=Model(),
        state=AgentRuntimeState(chat_id="c", user_id="u", run_id=run_id),
        middlewares=[make_adapter(*command_completion_hooks())])
    async for _ in agent.reply_stream(Msg(name="user", content=[TextBlock(type="text", text="start")], role="user")):
        pass
    assert len(seen) == 2
    assert any(row["task_id"] in text for text in seen[-1])
    assert store.snapshot(row["task_id"])["notice_status"] == "delivered"

async def test_idle_completion_uses_real_run_admission_and_executor(command_db, monkeypatch):
    from core.chat.sequencer import ChatSequencer
    pass
    import core.sandbox.commands.notifications as notifications
    from core.chat import service as chat_service
    from core.sandbox.commands import delivery
    from core.chat.operations import session_context
    monkeypatch.setattr(db_engine, "SessionLocal", command_db)
    with command_db() as db:
        accepted = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={})
        source_id = accepted.run.run_id
        source = db.get(models.ChatRun, source_id)
        source.status, source.writer_slot = "completed", None
        source.recovery_snapshot = {"kind":"chat","worker_args":{
            "context":{"model_provider_id":"original-provider","chat_mode":"careful","enable_thinking":True,
                       "sandbox_session_id":"c"}, "model_name":"original-model"}}
        db.commit()
    row = store.create(request(), "c", source_id)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0,"stdout":"done"})
    invocations = []
    async def worker(**kwargs):
        invocations.append(kwargs)
        with command_db() as db:
            run = db.get(models.ChatRun, kwargs["run_id"])
            run.status, run.writer_slot = "completed", None
            db.commit()
    monkeypatch.setattr(runtime_worker, "_run_workflow", worker)
    await notifications.dispatch()
    await asyncio.sleep(0.1)
    await notifications.dispatch()
    assert len(invocations) == 1
    assert invocations[0]["context"]["model_provider_id"] == "original-provider"
    assert invocations[0]["model_name"] == "original-model"
    assert row["task_id"] in invocations[0]["effective_user_message"]
    assert store.snapshot(row["task_id"])["notice_status"] == "delivered"

def test_bounded_output_reports_omitted_characters(command_db):
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0,
        "stdout":"x" * (store.LOG_CHARS + 50), "stderr":""})
    result = store.read(row["task_id"], "u", "c")
    assert len(result["stdout"]) == store.LOG_CHARS
    assert result["output_omitted_chars"] == 50
    assert store.read(row["task_id"], "u", "c")["output_omitted_chars"] == 0

async def test_recovery_preserves_terminal_result_and_cursor(command_db, monkeypatch):
    import core.sandbox.commands.runtime as module
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "dead", {"status":"running","session_id":"old"},
                 {"provider":"opensandbox","cursor":7})
    store.release_lease(row["task_id"], "dead")
    recover = AsyncMock(return_value={"status":"exited","session_id":None,
        "stdout":"remaining","stderr":"","exit_code":0,"execution_cursor":20})
    monkeypatch.setattr(commands_runtime, "recover_execution", recover)
    provider = SimpleNamespace(start_process=AsyncMock())
    runtime = CommandRuntime()
    await runtime.recover(provider)
    latest = store.snapshot(row["task_id"])
    assert latest["status"] == "completed"
    assert latest["stdout"] == "remaining"
    assert latest["execution_ref"]["cursor"] == 20
    assert recover.call_args.args[1]["cursor"] == 7
    provider.start_process.assert_not_called()

async def test_deadline_stops_real_process_without_relaunch(command_db, http_provider):
    runtime = CommandRuntime()
    req = request()
    req.script_content = "sleep 30"
    req.timeout = 1
    result = await runtime.start(http_provider, req, chat_id="c", source_run_id=None,
                                 background=True)
    await runtime.wait(result["task_id"], "u", "c", 5000)
    row = store.snapshot(result["task_id"])
    assert row["status"] == "failed"
    assert row["exit_code"] == 124 or "deadline" in row["error"].lower()
    await runtime.shutdown()

async def test_boundary_event_is_durable_before_model_call(command_db):
    from core.sandbox.commands import delivery
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0})
    run_id = source_run(command_db)
    delivery.reserve_boundary("c", "u", run_id)
    assert store.snapshot(row["task_id"])["notice_status"] == "delivered"
    with command_db() as db:
        assert row["task_id"] in str(db.get(models.ChatRun, run_id).recovery_snapshot)
        messages = db.query(models.ChatMessage).order_by(models.ChatMessage.chat_seq).all()
        assert [message.role for message in messages] == ["user", "assistant"]
        assert all(not (message.extra_data or {}).get("system_wake") for message in messages)

async def test_failed_model_request_keeps_durable_notice(command_db):
    run_id = source_run(command_db)
    from core.sandbox.commands.completion_hook import command_completion_hooks
    from tests.orchestration.hook_support import make_adapter
    from core.sandbox.commands import delivery
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0})
    agent = SimpleNamespace(state=SimpleNamespace(chat_id="c", user_id="u", run_id=run_id, context=[]))
    async def broken(**kwargs):
        raise ConnectionError("model down")
        yield
    with pytest.raises(ConnectionError):
        async for _ in make_adapter(*command_completion_hooks()).on_reasoning(agent, {}, broken): pass
    assert store.snapshot(row["task_id"])["notice_status"] == "delivered"
    with command_db() as db:
        assert row["task_id"] in str(db.get(models.ChatRun, run_id).recovery_snapshot)

async def test_command_http_authorization_and_nonconsuming_ui(command_db):
    import httpx
    from api.routes.v1.commands import router
    from core.auth.backend import UserContext, get_current_user
    from fastapi import FastAPI
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"exited","exit_code":0,"stdout":"result"}, None)
    app = FastAPI()
    app.include_router(router)
    current = ["u"]
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=current[0], user_center_id=current[0], username=current[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        for _ in range(2):
            response = await client.get(f"/v1/commands/{row['task_id']}/output")
            assert response.json()["data"]["stdout"] == "result"
        current[0] = "other"
        assert (await client.get(f"/v1/commands/{row['task_id']}/output")).status_code == 404
        assert (await client.post(f"/v1/commands/{row['task_id']}/cancel")).status_code == 404
        assert (await client.get("/v1/commands?chat_id=c")).json()["data"] == []

async def test_immediate_shutdown_stops_owned_process(command_db):
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}),
        write_stdin=AsyncMock(return_value={"status":"exited","exit_code":130}))
    runtime = CommandRuntime()
    result = await runtime.start(provider, request(), chat_id="c", source_run_id=None,
                                 background=True)
    await runtime.shutdown()
    assert store.snapshot(result["task_id"])["status"] == "cancelled"
    assert any(call.kwargs["chars"] == "\x03" for call in provider.write_stdin.call_args_list)
    assert not runtime.providers

async def test_cancel_does_not_publish_intermediate_terminal_notice(command_db, monkeypatch):
    import core.sandbox.commands.notifications as notifications
    stopped = asyncio.Event()
    release = asyncio.Event()
    async def poll(pid, **kwargs):
        if kwargs["chars"]:
            stopped.set()
            await release.wait()
            return {"status":"exited","exit_code":130,"stdout":"last"}
        await asyncio.sleep(0.02)
        return {"status":"running","session_id":"p"}
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}), write_stdin=poll)
    runtime = CommandRuntime()
    result = await runtime.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    cancelling = asyncio.create_task(runtime.cancel(result["task_id"], "u", "c"))
    await stopped.wait()
    await notifications.dispatch()
    assert not store.pending_notices()
    release.set()
    await cancelling
    row = store.snapshot(result["task_id"])
    assert (row["status"], row["notice_status"], row["stdout"]) == ("cancelled", "consumed", "last")
    await runtime.shutdown()

async def test_deleted_chat_keeps_monitor_until_stop_confirmed(command_db):
    from datetime import datetime, timezone
    calls = []
    async def poll(pid, **kwargs):
        calls.append(kwargs["chars"])
        if kwargs["chars"] or len(calls) == 1:
            return {"status":"running","session_id":"p","stdout":"part"}
        return {"status":"exited","exit_code":130,"stdout":"end"}
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(),
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}), write_stdin=poll)
    runtime = CommandRuntime()
    result = await runtime.start(provider, request(), chat_id="c", source_run_id=None, background=True)
    with command_db() as db:
        db.get(models.ChatSession, "c").deleted_at = datetime.now(timezone.utc)
        db.commit()
    await runtime.wait(result["task_id"], "u", "c", 2000)
    assert "\x03" in calls and calls[-1] == ""
    assert store.snapshot(result["task_id"])["status"] == "cancelled"
    await runtime.shutdown()

async def test_notification_pages_pass_fifty_blocked_events(command_db, monkeypatch):
    import core.sandbox.commands.notifications as notifications
    from core.chat.sequencer import ChatSequencer
    with command_db() as db:
        source = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="source", request_payload={}).run
        source.status, source.writer_slot = "completed", None
        source.recovery_snapshot = {"worker_args":{"context":{"chat_mode":"medium","enable_thinking":True}}}
        source_id = source.run_id
        db.commit()
    for index in range(51):
        row = store.create(request(), "c", source_id if index == 50 else None)
        tid = row["task_id"]
        store.attach(tid, "owner", {"status":"running","session_id":str(index)}, None)
        store.collect(tid, "owner", {"status":"exited","exit_code":0})
        with command_db() as db:
            db.get(models.CommandTask, tid).task_id = f"cmd_{index:03}"
            db.commit()
    launch = AsyncMock()
    monkeypatch.setattr(commands_notifications, "launch", launch)
    await notifications.dispatch()
    launch.assert_awaited_once()
    assert store.snapshot("cmd_050")["notice_status"] == "reserved"
