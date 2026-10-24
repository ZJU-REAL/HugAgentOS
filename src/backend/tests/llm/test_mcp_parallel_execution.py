"""Exercise agent scheduling through a real streamable-HTTP MCP server."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from agentscope.agent import Agent
from agentscope.mcp import HttpMCPConfig
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit
from mcp.server.fastmcp import FastMCP

from core.llm.mcp.manager import BareNameMCPClient


class ThreeCallsModel:
    model = "mcp-concurrency-probe"
    context_size = 32768

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
async def probe_server():
    intervals = {}
    server = FastMCP("parallel-probe", stateless_http=True, json_response=True)

    @server.tool(annotations={"readOnlyHint": True})
    async def probe(item: int) -> str:
        started = time.monotonic()
        await asyncio.sleep(0.4)
        intervals[item] = (started, time.monotonic())
        return f"completed-{item}"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="error"))
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
@pytest.mark.parametrize("stateful", [False, True])
async def test_agent_overlaps_real_mcp_requests(stateful):
    async with probe_server() as (url, intervals):
        client = BareNameMCPClient(
            name="parallel-probe",
            is_stateful=stateful,
            mcp_config=HttpMCPConfig(url=url),
        )
        try:
            await client.connect()
            # Enumeration and direct lookup must preserve the same policy.
            await client.list_tools()
            agent = Agent(
                name="probe-agent",
                system_prompt="Run the probes.",
                model=ThreeCallsModel(),
                toolkit=Toolkit(mcps=[client]),
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
            print(f"stateful={stateful} calls=3 elapsed={elapsed:.3f}s overlap={overlap:.3f}s")
            assert overlap > 0.2, "MCP requests did not execute concurrently"
            assert "done" in str(result)
        finally:
            await client.close()
