"""Run handles, the shared STDIO pool, call admission and result shaping over real SDK transports."""
import asyncio
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from mcp import types
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError

from core.llm.mcp import chat_sessions
from core.llm.mcp.callbacks import Interactions
from core.llm.mcp.lifetime import discover
from core.llm.mcp.tools import queue_progress
from tests.llm.test_mcp_parallel_execution import probe_server


def _state(chunk):
    return str(getattr(chunk.state, "value", chunk.state))


def _texts(chunk):
    return [block.text for block in chunk.content if isinstance(block, TextBlock)]


def _run(answer, chat="pool-chat", user="pool-user"):
    class Model:
        model = "fixture"
        async def __call__(self, messages, tools=None, **kwargs):
            return ChatResponse(content=[TextBlock(text=answer)], is_last=True)
    request = SimpleNamespace(chat_id=chat, run_id="", current_user_id=user)
    return Interactions(request, "pool", True,
                        agent=SimpleNamespace(model=Model(), state=SimpleNamespace(pending_mcp_progress=[])))


def _effects_server(calls):
    server = MCPServer("effects")

    @server.tool(annotations={"readOnlyHint": True})
    async def look(seconds: float = 0) -> str:
        await asyncio.sleep(seconds)
        return "seen"

    @server.tool()
    async def write(key: str, seconds: float = 0) -> str:
        calls.append(key)
        await asyncio.sleep(seconds)
        return "written " + key

    @server.tool(annotations={"readOnlyHint": True})
    async def reject() -> str:
        raise MCPError(code=-32602, message="bad request")

    @server.tool(annotations={"readOnlyHint": True})
    def typed(text: str) -> Dict[str, Any]:
        return {"text": text}

    @server.tool(annotations={"readOnlyHint": True})
    async def structured_only() -> types.CallToolResult:
        return types.CallToolResult(content=[], structured_content={"answer": 42})

    @server.tool(annotations={"readOnlyHint": True})
    async def described() -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(text="see data")], structured_content={"answer": 42})

    @server.tool(annotations={"readOnlyHint": True})
    def plain() -> str:
        return "done"

    @server.tool(annotations={"readOnlyHint": True})
    async def steps(ctx: Context) -> str:
        for step in range(1, 6):
            await ctx.report_progress(step, 5, f"step {step}")
        return "stepped"
    return server


@pytest.mark.asyncio
async def test_request_failures_become_results_and_keep_the_session():
    calls = []
    async with probe_server(_effects_server(calls)) as (url, _):
        resource = await discover("effects", {"transport": "streamable_http", "url": url, "execution_timeout": 0.5})
        tools = {tool.name: tool for tool in resource.tools}
        try:
            timed_out = await tools["look"](seconds=2)
            assert _state(timed_out) == "error" and "outcome_unknown" not in timed_out.metadata
            rejected = await tools["reject"]()
            assert _state(rejected) == "error"
            assert not resource.session.failed
            assert _texts(await tools["look"]()) == ["seen"]
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_write_with_unknown_outcome_is_never_replayed_in_the_same_run():
    calls = []
    async with probe_server(_effects_server(calls)) as (url, _):
        cfg = {"transport": "streamable_http", "url": url, "execution_timeout": 0.5}
        resource = await discover("effects", cfg)
        tools = {tool.name: tool for tool in resource.tools}
        try:
            first = await tools["write"](key="a", seconds=2)
            assert first.metadata["outcome_unknown"] is True
            replay = await tools["write"](key="a", seconds=2)
            assert replay.metadata["outcome_unknown"] is True
            assert calls == ["a"]
            assert _texts(await tools["write"](key="b")) == ["written b"]
        finally:
            await resource.close()
        # A new run decides for itself.
        later = await discover("effects", cfg)
        try:
            assert _texts(await {t.name: t for t in later.tools}["write"](key="a")) == ["written a"]
        finally:
            await later.close()


@pytest.mark.asyncio
async def test_read_only_calls_overlap_while_writes_run_alone():
    calls = []
    async with probe_server(_effects_server(calls)) as (url, _):
        resource = await discover("effects", {"transport": "streamable_http", "url": url})
        tools = {tool.name: tool for tool in resource.tools}
        try:
            assert tools["look"].is_concurrency_safe and not tools["write"].is_concurrency_safe
            started = time.monotonic()
            await asyncio.gather(*(tools["look"](seconds=0.4) for _ in range(4)))
            assert time.monotonic() - started < 1.0
            started = time.monotonic()
            await asyncio.gather(*(tools["write"](key=str(i), seconds=0.3) for i in range(3)))
            assert time.monotonic() - started >= 0.85
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_structured_output_reaches_the_model_once():
    async with probe_server(_effects_server([])) as (url, _):
        resource = await discover("effects", {"transport": "streamable_http", "url": url})
        tools = {tool.name: tool for tool in resource.tools}
        try:
            typed = await tools["typed"](text="x")
            assert len(_texts(typed)) == 1 and "structuredContent" not in typed.metadata["mcp"]
            assert _texts(await tools["structured_only"]()) == ['{"answer":42}']
            assert _texts(await tools["plain"]()) == ["done"]
            # Text that does not carry the structured value keeps it for the model.
            assert _texts(await tools["described"]()) == ["see data", '{"answer":42}']
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_progress_keeps_the_newest_event_per_call():
    queue = []
    for step in range(3):
        queue_progress(queue, {"tool_id": "a", "progress": step})
    queue_progress(queue, {"tool_id": "b", "progress": 9})
    assert queue == [{"tool_id": "a", "progress": 2}, {"tool_id": "b", "progress": 9}]

    run = _run("unused")
    async with probe_server(_effects_server([]), json_response=False) as (url, _):
        resource = await discover("effects", {"transport": "streamable_http", "url": url}, interactions=run)
        try:
            await {t.name: t for t in resource.tools}["steps"]()
            events = run.agent.state.pending_mcp_progress
            assert len(events) == 1 and events[0]["progress"] == 5
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_listings_are_shared_across_runs_of_one_authorization_context():
    server = MCPServer("cached", cache_hints={method: CacheHint(ttl_ms=60_000) for method in
                                               ("server/discover", "tools/list", "resources/list",
                                                "resources/templates/list", "prompts/list")})

    @server.tool(annotations={"readOnlyHint": True})
    def ping() -> str:
        return "pong"

    async with probe_server(server) as (url, _):
        first = await _timed_listing(url, "alice")
        second = await _timed_listing(url, "alice")
        other = await _timed_listing(url, "bob")
    assert second < first and other == first


async def _timed_listing(url, user):
    """Count list requests one run sends, through the SDK's own session methods."""
    from mcp.client.session import ClientSession
    sent = []
    original = ClientSession.send_request

    async def counting(self, request, *args, **kwargs):
        sent.append(getattr(request, "method", type(request).__name__))
        return await original(self, request, *args, **kwargs)

    ClientSession.send_request = counting
    try:
        resource = await discover("cached", {"transport": "streamable_http", "url": url,
                                             "headers": {"X-Current-User-Id": user}})
        await resource.close()
    finally:
        ClientSession.send_request = original
    return len(sent)


def _stdio_server(tmp_path):
    script = tmp_path / "pool_server.py"
    script.write_text(
        "import os\n"
        "from mcp import types\n"
        "from mcp.server.mcpserver import Context, MCPServer\n"
        "s = MCPServer('pool')\n"
        "@s.tool(annotations={'readOnlyHint': True})\n"
        "def pid() -> int:\n    return os.getpid()\n"
        "@s.tool(annotations={'readOnlyHint': True})\n"
        "async def ask(ctx: Context) -> str | types.InputRequiredResult:\n"
        "    if ctx.input_responses:\n"
        "        return ctx.input_responses['q'].content.text\n"
        "    return types.InputRequiredResult(input_requests={'q': types.CreateMessageRequest(\n"
        "        params=types.CreateMessageRequestParams(messages=[types.SamplingMessage(\n"
        "            role='user', content=types.TextContent(text='who?'))], max_tokens=10))})\n"
        "s.run()\n")
    return {"transport": "stdio", "command": sys.executable, "args": [str(script)]}


@pytest.mark.asyncio
async def test_concurrent_runs_share_one_process_and_each_answers_its_own_requests(tmp_path):
    cfg = _stdio_server(tmp_path)
    scope = ("pool-user", "pool-chat", "")
    first = await discover("pool", cfg, interactions=_run("first-run"), scope=scope)
    second = await asyncio.wait_for(discover("pool", cfg, interactions=_run("second-run"), scope=scope), 5)
    try:
        assert first.session is second.session
        for run in (first, second):
            run.interactions.agent.toolkit = None
        answers = await asyncio.gather(
            {t.name: t for t in first.tools}["ask"](), {t.name: t for t in second.tools}["ask"]())
        assert [_texts(a) for a in answers] == [["first-run"], ["second-run"]]
    finally:
        await first.close()
        await second.close()
        await chat_sessions.close_chat()


@pytest.mark.asyncio
async def test_idle_sessions_make_room_and_only_held_sessions_count_against_capacity(tmp_path, monkeypatch):
    cfg = _stdio_server(tmp_path)
    monkeypatch.setattr(chat_sessions, "MAX_USER_SESSIONS", 2)
    try:
        for chat in range(4):
            resource = await discover("pool", cfg, scope=("cap-user", f"chat-{chat}", ""))
            assert _state(await resource.tools[0]()) != "error"
            await resource.close()
            assert sum(k[0] == "cap-user" for k in chat_sessions._entries) <= 2
        held = [await discover("pool", cfg, scope=("cap-user", f"held-{i}", "")) for i in range(2)]
        try:
            with pytest.raises(RuntimeError, match="capacity"):
                await discover("pool", cfg, scope=("cap-user", "one-too-many", ""))
        finally:
            for resource in held:
                await resource.close()
    finally:
        await chat_sessions.close_chat()
    assert not chat_sessions._entries
