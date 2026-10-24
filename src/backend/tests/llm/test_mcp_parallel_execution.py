"""Exercise agent scheduling through a real streamable-HTTP MCP server."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit
from mcp.server.mcpserver import MCPServer

from core.llm.mcp.lifetime import discover


class ThreeCallsModel:
    model = "mcp-concurrency-probe"
    context_size = 32768
    from agentscope.formatter import OpenAIChatFormatter
    formatter = OpenAIChatFormatter()

    def __init__(self):
        self.calls = 0

    async def count_tokens(self, messages, tools=None):
        return 1

    async def __call__(self, messages, tools=None, **kwargs):
        self.calls += 1
        content = (
            [
                ToolCallBlock(id=f"call-{i}", name="probe", input='{"item": %d}' % i)
                for i in range(3)
            ]
            if self.calls == 1
            else [TextBlock(text="done")]
        )
        return ChatResponse(content=content, is_last=True)


@asynccontextmanager
async def probe_server(mcp_server=None, *, json_response=True):
    intervals = {}
    server = mcp_server or MCPServer("parallel-probe")

    async def probe(item: int) -> str:
        started = time.monotonic()
        await asyncio.sleep(0.4)
        intervals[item] = (started, time.monotonic())
        return f"completed-{item}"

    if mcp_server is None:
        server.tool(annotations={"readOnlyHint": True})(probe)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(server.streamable_http_app(stateless_http=True, json_response=json_response), log_level="error"))
    task = asyncio.create_task(http.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not http.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp", intervals
    finally:
        http.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@pytest.mark.asyncio
async def test_agent_overlaps_real_mcp_requests():
    async with probe_server() as (url, intervals):
        client = await discover("parallel-probe", {"transport": "streamable_http", "url": url})
        try:
            tools = client.tools
            # The probe is annotated read-only, so the agent may overlap its calls.
            assert all(tool.is_concurrency_safe for tool in tools)
            agent = Agent(
                name="probe-agent",
                system_prompt="Run the probes.",
                model=ThreeCallsModel(),
                toolkit=Toolkit(tools=tools),
            )
            started = time.monotonic()
            result = await agent.reply(
                UserMsg(name="user", content="Run three independent probes.")
            )
            elapsed = time.monotonic() - started
            assert set(intervals) == {0, 1, 2}
            overlap = min(end for _, end in intervals.values()) - max(
                start for start, _ in intervals.values()
            )
            print(f"calls=3 elapsed={elapsed:.3f}s overlap={overlap:.3f}s")
            assert overlap > 0.2, "MCP requests did not execute concurrently"
            assert "done" in str(result)
        finally:
            await client.close()
