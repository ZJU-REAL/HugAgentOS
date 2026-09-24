"""Public tool schema and process-session behavior."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.llm.tools.sandbox_tool import register_bash


class Toolkit:
    def __init__(self):
        self.functions = {}

    def register_tool_function(self, fn, **kwargs):
        self.functions[fn.__name__] = fn


def payload(result):
    block = result.content[0]
    return json.loads(block["text"] if isinstance(block, dict) else block.text)


async def test_bash_yields_and_write_stdin_waits_on_same_owned_process(monkeypatch):
    provider = SimpleNamespace(
        runs_on_host=False,
        start_process=AsyncMock(
            return_value={
                "status": "running",
                "session_id": "p1",
                "stdout": "first",
                "stderr": "",
                "exit_code": None,
            }
        ),
        write_stdin=AsyncMock(
            return_value={
                "status": "exited",
                "session_id": None,
                "stdout": "second",
                "stderr": "",
                "exit_code": 7,
            }
        ),
    )
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: provider)
    toolkit = Toolkit()
    register_bash(toolkit, loader=None, loaded_skill_ids=set(), chat_id="chat-a")
    assert "write_stdin" in toolkit.functions
    first = payload(await toolkit.functions["bash"]("long-command", yield_time_ms=250))
    assert first["session_id"] == "p1"
    req = provider.start_process.call_args.args[0]
    assert req.timeout is None
    assert req.session_id == "chat-a"
    second = payload(await toolkit.functions["write_stdin"]("p1", yield_time_ms=1000))
    assert second["stdout"] == "second"
    assert second["exit_code"] == 7
    assert provider.start_process.await_count == 1
    assert provider.write_stdin.call_args.kwargs["sandbox_session_id"] == "chat-a"


@pytest.mark.parametrize("lost_handle", [False, True])
async def test_team_blocks_new_prepare_until_previous_source_sync_finishes(
    monkeypatch, lost_handle
):
    import asyncio
    from core.sandbox.errors import SandboxError

    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(*args):
        entered.set()
        await release.wait()
        return 1

    provider = SimpleNamespace(
        runs_on_host=False,
        start_process=AsyncMock(
            return_value={
                "status": "running",
                "session_id": "team-p1",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
        ),
        write_stdin=AsyncMock(
            side_effect=SandboxError("Unknown process session") if lost_handle else None,
            return_value={
                "status": "exited",
                "session_id": None,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
            },
        ),
    )
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: provider)
    monkeypatch.setattr(
        "core.llm.tools.project_source_access.current_scope_error", lambda *a, **kw: None
    )
    prepare = AsyncMock(return_value=[("file.txt", b"original")])
    monkeypatch.setattr("core.llm.tools.project_working_copy.prepare", prepare)
    monkeypatch.setattr("core.llm.tools.project_working_copy.persist", persist)
    toolkit = Toolkit()
    register_bash(
        toolkit,
        loader=None,
        loaded_skill_ids=set(),
        chat_id="team-chat",
        scope=SimpleNamespace(kind="team", project_id="project-a"),
    )
    started = payload(await toolkit.functions["bash"]("echo edit"))
    finishing = asyncio.create_task(
        toolkit.functions["write_stdin"](started["session_id"], yield_time_ms=0)
    )
    await entered.wait()
    blocked = payload(await toolkit.functions["bash"]("echo overwrite"))
    assert "error" in blocked
    assert prepare.await_count == 1
    release.set()
    completed = payload(await finishing)
    assert completed["project_synced_count"] == 1
    # Once persistence completes, another command can prepare its working copy.
    started = payload(await toolkit.functions["bash"]("echo next"))
    assert started["status"] == "running"
    await toolkit.functions["write_stdin"](started["session_id"], yield_time_ms=0)


async def test_registered_schemas_expose_two_tool_contract(monkeypatch):
    from agentscope.tool import Toolkit as AgentToolkit
    from core.llm.tool_collector import ToolCollector

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    collector = ToolCollector()
    register_bash(collector, loader=None, loaded_skill_ids=set(), chat_id="schema-test")
    schemas = {
        s["function"]["name"]: s["function"]
        for s in await AgentToolkit(tools=collector.function_tools).get_tool_schemas()
    }
    assert {"bash", "write_stdin", "Bash"} <= schemas.keys()
    assert "run_to_completion" not in schemas
    assert "execute" not in schemas
    assert schemas["bash"]["parameters"]["properties"]["yield_time_ms"]["default"] == 60000
    assert "timeout" not in schemas["bash"]["parameters"]["required"]
    assert schemas["write_stdin"]["parameters"]["properties"]["chars"]["default"] == ""
    assert schemas["write_stdin"]["parameters"]["required"] == ["session_id"]
