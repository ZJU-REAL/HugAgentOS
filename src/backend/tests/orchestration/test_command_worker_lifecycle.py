"""Worker ownership and shutdown behavior of background commands."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.sandbox.commands.runtime as commands_runtime
from core.sandbox.commands.runtime import CommandRuntime
from tests.orchestration.test_background_commands import command_db, request


async def test_worker_initialization_survives_first_call_from_temporary_thread(command_db, monkeypatch):
    from api.startup import commands
    runtime = CommandRuntime()
    async def poll(pid, **kwargs):
        return {"status":"exited","exit_code":130} if kwargs["chars"] else {
            "status":"running","session_id":"p"}
    provider = SimpleNamespace(name="test", touch_session=AsyncMock(), write_stdin=poll,
        start_process=AsyncMock(return_value={"status":"running","session_id":"p"}))
    monkeypatch.setattr(commands, "runtime", runtime)
    monkeypatch.setattr(commands, "get_sandbox_provider", lambda: provider)
    await commands.startup_commands()
    try:
        service_loop = asyncio.get_running_loop()
        result = await asyncio.to_thread(lambda: asyncio.run(runtime.start(provider,
            request(), chat_id="c", source_run_id=None, background=True)))
        assert runtime.loop is service_loop
        assert result["task_id"] in runtime.tasks
        assert not runtime.tasks[result["task_id"]].done()
        provider.start_process.return_value = {"status":"exited","exit_code":0,"stdout":"main"}
        assert (await runtime.start(provider, request(), chat_id="c", source_run_id=None))["stdout"] == "main"
    finally:
        await commands.shutdown_commands()
    assert runtime.loop is None
    assert not runtime.tasks

async def test_lifespan_starts_and_stops_commands_in_each_worker(command_db, monkeypatch):
    from api import lifecycle
    from api.startup import commands
    from core.infra import leader
    from features.desktop.gateway import observability as desktop_gateway_observability
    actual = next(row for row in lifecycle._startup_steps() if row[0] is lifecycle.startup_commands)
    assert actual[4] == lifecycle._PER_WORKER
    runtime = CommandRuntime()
    monkeypatch.setattr(commands, "runtime", runtime)
    monkeypatch.setattr(commands, "get_sandbox_provider", lambda: SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_startup_steps", lambda: (
        (commands.startup_commands, commands.shutdown_commands, True,
            lifecycle._ALL_ROLES, lifecycle._PER_WORKER),))
    leadership = SimpleNamespace(start=lambda **kw: None, stop=AsyncMock())
    monkeypatch.setattr(leader, "Leadership", lambda name: leadership)
    monkeypatch.setattr(leader, "coordination_required", lambda: False)
    for name in dir(lifecycle):
        if name.startswith("_shutdown_"):
            monkeypatch.setattr(lifecycle, name, AsyncMock())
    monkeypatch.setattr(desktop_gateway_observability, "drain", AsyncMock())
    async with lifecycle.lifespan(SimpleNamespace(state=SimpleNamespace())):
        assert runtime.loop is asyncio.get_running_loop()
        assert commands._task is not None
    assert commands._task is None
    assert runtime.loop is None

async def test_managed_read_never_holds_model_for_long_wait(command_db, monkeypatch):
    import core.sandbox.commands.runtime as module
    from core.llm.tools.sandbox_tool import register_bash
    from core.sandbox.commands import store
    from tests.llm.test_bash_process_tools import Toolkit
    row = store.create(request(), "c", None)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    instance = SimpleNamespace(wait=AsyncMock())
    monkeypatch.setattr(commands_runtime, "runtime", instance)
    from core.llm.ports import process_state
    monkeypatch.setattr(process_state(), "commands", instance)
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    tools = Toolkit()
    register_bash(tools, loader=None, chat_id="c", user_id="u")
    await tools.functions["write_stdin"](row["task_id"], yield_time_ms=100000)
    instance.wait.assert_awaited_once_with(row["task_id"], "u", "c", 3000)
    instance.wait.reset_mock()
    await tools.functions["write_stdin"](row["task_id"])
    instance.wait.assert_awaited_once_with(row["task_id"], "u", "c", 0)
