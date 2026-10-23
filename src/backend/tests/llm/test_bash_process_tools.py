"""Public tool schema and process-session behavior."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.llm.tools.sandbox_tool import register_bash


class Toolkit:
    def __init__(self):
        self.functions = {}

    def append(self, tool):
        fn = tool._func
        kwargs = {"func_description": tool.description, "permission": getattr(tool, "dispatch_spec", None)}
        self.functions[fn.__name__] = fn


def payload(result):
    block = result.content[0]
    return json.loads(block["text"] if isinstance(block, dict) else block.text)


async def test_bash_yields_and_write_stdin_waits_on_same_owned_process(monkeypatch, tmp_path):
    from core.db.engine import Base
    from core.db.models import ChatSession, UserShadow
    from core.sandbox.commands.runtime import runtime
    from core.sandbox.commands import store
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///" + str(tmp_path / "tools.sqlite"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(store, "SessionLocal", factory)
    with factory() as db:
        db.add(UserShadow(user_id="u", username="test"))
        db.flush()
        db.add(ChatSession(chat_id="chat-a", user_id="u", title="test"))
        db.commit()

    provider = SimpleNamespace(
        name="script_runner", touch_session=AsyncMock(), runs_on_host=False,
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
    register_bash(toolkit, loader=None, chat_id="chat-a", user_id="u")
    assert "write_stdin" in toolkit.functions
    first = payload(await toolkit.functions["Bash"]("long-command", yield_time_ms=250))
    assert first["session_id"].startswith("cmd_")
    req = provider.start_process.call_args.args[0]
    assert req.timeout is None
    assert req.session_id == "chat-a"
    second = payload(await toolkit.functions["write_stdin"](first["session_id"], yield_time_ms=1000))
    assert second["stdout"] == "second"
    assert second["exit_code"] == 7
    assert provider.start_process.await_count == 1
    assert provider.write_stdin.call_args.kwargs["sandbox_session_id"] == "chat-a"
    await runtime.shutdown()
    engine.dispose()


async def test_registered_schemas_expose_two_tool_contract(monkeypatch):
    from agentscope.tool import Toolkit as AgentToolkit
    from core.llm.factory.tools.definitions import ToolAssembly

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    collector = ToolAssembly()
    register_bash(collector, loader=None, chat_id="schema-test")
    schemas = {
        s["function"]["name"]: s["function"]
        for s in await AgentToolkit(tools=list(collector)).get_tool_schemas()
    }
    assert set(schemas) == {"Bash", "write_stdin", "background_process", "list_processes"}
    assert schemas["Bash"]["parameters"]["properties"]["run_in_background"]["default"] is False
    assert "run_to_completion" not in schemas
    assert "execute" not in schemas
    assert schemas["Bash"]["parameters"]["properties"]["yield_time_ms"]["default"] == 60000
    assert "timeout" not in schemas["Bash"]["parameters"]["required"]
    assert schemas["write_stdin"]["parameters"]["properties"]["yield_time_ms"]["default"] == 0
    assert schemas["write_stdin"]["parameters"]["properties"]["chars"]["default"] == ""
    assert schemas["write_stdin"]["parameters"]["required"] == ["session_id"]
