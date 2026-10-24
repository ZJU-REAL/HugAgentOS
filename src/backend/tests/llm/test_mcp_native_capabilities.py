"""Native modern protocol and business boundaries over real HTTP and STDIO."""
import asyncio
import os
import sys
from types import SimpleNamespace
import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from mcp import types
from mcp.server.mcpserver import MCPServer, Context
from core.llm.mcp.lifetime import discover
from core.llm.mcp.callbacks import Interactions
from core.llm.mcp import chat_sessions
from core.llm.mcp.chat_sessions import close_chat
from tests.llm.test_mcp_parallel_execution import probe_server


def _text(chunk):
    return "".join(block.text for block in chunk.content if isinstance(block, TextBlock))


def interactions():
    class Model:
        model = "fixture"
        async def __call__(self, messages, tools=None, **kwargs):
            assert tools is None
            assert kwargs["max_tokens"] == 10
            assert "private-history" not in str(messages)
            return ChatResponse(content=[TextBlock(text="Paris")], is_last=True)
    request = SimpleNamespace(chat_id="mcp-chat", run_id="", current_user_id="mcp-user")
    binding = Interactions(request, "probe", True)
    binding.agent = SimpleNamespace(model=Model(), state=SimpleNamespace(pending_mcp_progress=[]))
    return binding


def call_scope():
    """One active outer call of a fresh run binding."""
    return interactions().open_call()


@pytest.mark.asyncio
async def test_modern_sampling_elicitation_progress_resources_and_prompts(monkeypatch):
    server = MCPServer("modern")
    @server.tool()
    async def sample(ctx: Context) -> str | types.InputRequiredResult:
        if ctx.input_responses:
            return ctx.input_responses["q"].content.text
        return types.InputRequiredResult(input_requests={"q": types.CreateMessageRequest(
            params=types.CreateMessageRequestParams(messages=[types.SamplingMessage(
                role="user", content=types.TextContent(text="Capital of France?"))], max_tokens=10))})
    @server.tool()
    async def form(ctx: Context) -> str | types.InputRequiredResult:
        if ctx.input_responses:
            answer = ctx.input_responses["q"]
            assert answer.content == {"count": 3, "enabled": True}
            return "accepted"
        return types.InputRequiredResult(input_requests={"q": types.ElicitRequest(params=types.ElicitRequestFormParams(
            message="Choose", requested_schema={"type": "object", "properties": {
                "count": {"type": "integer", "minimum": 1}, "enabled": {"type": "boolean"}},
                "required": ["count", "enabled"]}))})
    @server.tool()
    async def progress(ctx: Context) -> str:
        await ctx.report_progress(1, 2, "halfway")
        return "done"
    @server.tool()
    async def linked() -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(text="description"),
            types.ResourceLink(name="report", uri="report://one", mime_type="text/plain")],
            structured_content={"answer": 42})
    @server.resource("report://one")
    def report() -> str:
        return "resource body"
    @server.resource("report://by-name/{name}")
    def report_template(name: str) -> str:
        return "template " + name
    @server.prompt()
    def summarize(subject: str) -> str:
        return "untrusted prompt " + subject
    from core.llm.tools import user_questions as uq
    async def answer_form():
        for _ in range(500):
            queue = uq.get_ui_queue("mcp-chat")
            if queue and not queue.empty():
                event = queue.get_nowait()
                if event["event"] == "requested":
                    assert uq.answer("mcp-chat", event["request_id"], [
                        {"id":"count", "custom":"3"}, {"id":"enabled", "selected":["option_1"]}
                    ])["ok"]
                    return
            await asyncio.sleep(.01)
        raise AssertionError("MCP elicitation did not reach the user-question queue")
    binding = interactions()
    async with probe_server(server, json_response=False) as (url, _):
        resource = await discover("modern", {"transport": "streamable_http", "url": url}, interactions=binding)
        try:
            assert resource.session.client.protocol_version == "2026-07-28"
            tools = {t.name: t for t in resource.tools}
            assert "Paris" in str(await tools["sample"]())
            answer_task = asyncio.create_task(answer_form())
            try:
                assert "accepted" in str(await tools["form"]())
                await answer_task
            finally:
                answer_task.cancel()
                await asyncio.gather(answer_task, return_exceptions=True)
            await tools["progress"]()
            assert binding.agent.state.pending_mcp_progress[0]["progress"] == 1
            result = await tools["linked"]()
            assert '{"answer":42}' in str(result.content) and "structuredContent" not in result.metadata["mcp"]
            assert "report://one" in str(result.content)
            resource_tool = next(t for t in resource.tools if t.name.startswith("mcp_resource_"))
            assert "resource body" in str(await resource_tool())
            template_tool = next(t for t in resource.tools if t.name.startswith("mcp_resource_template_"))
            assert "template one" in str(await template_tool(name="one"))
            prompt_tool = next(t for t in resource.tools if t.name.startswith("mcp_prompt_"))
            assert "untrusted prompt topic" in str(await prompt_tool(subject="topic"))
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_stdio_chat_reuse_isolated_users_stale_lease_and_cancelled_opener(tmp_path):
    script = tmp_path / "pid.py"
    script.write_text('import os\nfrom mcp.server.mcpserver import MCPServer\ns=MCPServer("pid")\n@s.tool()\ndef pid()->int:return os.getpid()\ns.run()\n')
    cfg = {"transport": "stdio", "command": sys.executable, "args": [str(script)]}
    scope = ("user-one", "chat", "")
    first = await discover("pid", cfg, scope=scope)
    old_tool = first.tools[0]
    pid = _text(await old_tool())
    await first.close()
    second = await discover("pid", cfg, scope=scope)
    try:
        assert _text(await second.tools[0]()) == pid
        with pytest.raises(RuntimeError, match="lease is closed"):
            await old_tool()
        # A concurrent run in the same chat shares the live process instead of waiting.
        concurrent = await asyncio.wait_for(discover("pid", cfg, scope=scope), 5)
        assert _text(await concurrent.tools[0]()) == pid
        await concurrent.close()
        assert _text(await second.tools[0]()) == pid
        # A run cancelled while its process opens leaves a parked session, not a leak.
        opener = asyncio.create_task(discover("pid", cfg, scope=("user-one", "other-chat", "")))
        await asyncio.sleep(0.02)
        opener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opener
        reused = await discover("pid", cfg, scope=("user-one", "other-chat", ""))
        try:
            assert sum(k[1] == "other-chat" for k in chat_sessions._entries) == 1
        finally:
            await reused.close()
        third = await discover("pid", cfg, scope=("user-two", "chat", ""))
        try:
            assert _text(await third.tools[0]()) != pid
        finally:
            await third.close()
    finally:
        await second.close()
        await close_chat()
    assert second.session._owner.done()


@pytest.mark.asyncio
async def test_sampling_denies_excessive_or_shared_context():
    call = call_scope()
    params = types.CreateMessageRequestParams(messages=[], max_tokens=5000)
    assert isinstance(await call.sampling(None, params), types.ErrorData)
    params.max_tokens = 10
    params.include_context = "allServers"
    assert isinstance(await call.sampling(None, params), types.ErrorData)
    call.run.interactive = False
    form = types.ElicitRequestFormParams(message="test", requested_schema={"type":"object"})
    assert (await call.elicitation(None, form)).action == "decline"
