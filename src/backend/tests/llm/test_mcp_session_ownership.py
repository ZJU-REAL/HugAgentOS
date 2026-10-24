"""Real SDK transports: one discovery, run ownership, user isolation and cancellation."""
import asyncio
import sys
import pytest
from agentscope.message import ToolCallBlock
from agentscope.tool import Toolkit
from core.llm.mcp.lifetime import discover
from core.llm.runtime.state import AgentRuntimeState
from tests.llm.test_mcp_parallel_execution import probe_server


@pytest.mark.asyncio
async def test_http_tools_are_discovered_once_and_read_only_tools_may_overlap():
    async with probe_server() as (url, intervals):
        resource = await discover("probe", {"transport": "streamable_http", "url": url})
        try:
            assert [t.name for t in resource.tools] == ["probe"]
            assert resource.tools[0].is_concurrency_safe is True
            toolkit = Toolkit(tools=resource.tools)
            for _ in range(2):
                assert (await toolkit.get_tool_schemas())[0]["function"]["name"] == "probe"
            chunks = await resource.tools[0](item=8)
            assert "completed-8" in str(chunks)
            assert list(intervals) == [8]
        finally:
            await resource.close()
        assert resource.session._owner.done()


@pytest.mark.asyncio
async def test_stdio_discovery_and_calls_can_cross_tasks_without_cross_task_cleanup(tmp_path):
    script = tmp_path / "server.py"
    script.write_text('from mcp.server.mcpserver import MCPServer\ns=MCPServer("owned")\n@s.tool()\ndef echo(text: str)->str: return text\ns.run()\n')
    cfg = {"command": sys.executable, "args": [str(script)]}
    resource = await discover("owned", cfg)
    async def call():
        return await resource.tools[0](text="run-owned")
    assert "run-owned" in str(await asyncio.create_task(call()))
    await asyncio.create_task(resource.close())
    assert resource.session._owner.done()
    assert not resource.session._owner.cancelled()


@pytest.mark.asyncio
async def test_failed_discovery_releases_owner(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text('raise SystemExit(1)\n')
    with pytest.raises(Exception):
        await discover("bad", {"command": sys.executable, "args": [str(script)]})
    assert not any(t.get_name() == "mcp-session:bad" and not t.done() for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_cancel_never_ready_stdio_does_not_leave_an_owner(tmp_path):
    script = tmp_path / "never_ready.py"
    script.write_text('import time\ntime.sleep(300)\n')
    task = asyncio.create_task(discover("never-ready", {
        "command": sys.executable, "args": [str(script)],
    }))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not any(t.get_name() == "mcp-session:never-ready" and not t.done() for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_run_guard_handles_native_tools_and_revoked_mcp():
    from agentscope.tool import FunctionTool
    from core.llm.tools.ontology_toolkit import OntologyFilteredToolkit
    calls = []
    async def local(value: str):
        calls.append(value)
        return value
    native = FunctionTool(local)
    async with probe_server() as (url, intervals):
        resource = await discover("probe", {"transport": "streamable_http", "url": url})
        try:
            toolkit = OntologyFilteredToolkit(tools=[native, *resource.tools])
            allowed = True
            toolkit.capability_guard = lambda name: allowed and name == "probe"
            state = AgentRuntimeState()
            result = [x async for x in toolkit.call_tool(ToolCallBlock(id="native", name="local", input='{"value":"local-ok"}'), state)]
            assert calls == ["local-ok"]
            result = [x async for x in toolkit.call_tool(ToolCallBlock(id="allowed", name="probe", input='{"item":1}'), state)]
            assert 1 in intervals
            allowed = False
            result = [x async for x in toolkit.call_tool(ToolCallBlock(id="revoked", name="probe", input='{"item":2}'), state)]
            assert 2 not in intervals
            assert "本次调用未执行" in str(result)
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_plugin_rollback_repeated_cancel_releases_every_stdio_owner(tmp_path):
    from types import SimpleNamespace
    from core.llm.tools.plugin_activation import _ActivationStage
    script = tmp_path / "delayed_shutdown.py"
    script.write_text('''import anyio
from contextlib import asynccontextmanager
from mcp.server.mcpserver import MCPServer
@asynccontextmanager
async def lifespan(server):
    try:
        yield {}
    finally:
        await anyio.sleep(.3)
s = MCPServer("rollback", lifespan=lifespan)
@s.tool()
def echo(text: str)->str: return text
s.run()
''')
    live = SimpleNamespace(tools=[], skills_or_loaders=[])
    stage = _ActivationStage(live, {"close_list": []}, None)
    resources = [await discover("rollback-"+str(i), {"command":sys.executable,"args":[str(script)]}) for i in range(2)]
    for resource in resources:
        stage.add_client(resource.name, resource)
    cleanup = asyncio.create_task(stage.discard())
    await asyncio.sleep(.05)
    cleanup.cancel()
    await asyncio.sleep(.05)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cleanup, 10)
    assert all(r.session._stop.is_set() and r.session._owner.done() for r in resources)
    assert not stage._staged_clients
    assert not live.tools
