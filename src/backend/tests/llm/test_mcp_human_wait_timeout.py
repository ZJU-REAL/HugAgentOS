"""Human waits do not consume the active MCP invocation budget."""
import asyncio
import uuid
import json
import socket
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

import pytest
from mcp import types
from mcp.server.mcpserver import Context, MCPServer

from core.llm.mcp.lifetime import discover
from core.llm.tools import user_questions as uq
from tests.llm.test_mcp_parallel_execution import probe_server
from tests.llm.test_mcp_runtime_pool import _run


def question_server():
    server = MCPServer("human-budget")

    @server.tool()
    async def choose(ctx: Context, before: float = 0, after: float = 0, questions: int = 1) -> str | types.InputRequiredResult:
        if ctx.input_responses:
            await asyncio.sleep(after)
            answer = ctx.input_responses["choice"]
            return answer.action + ":" + str((answer.content or {}).get("color", ""))
        await asyncio.sleep(before)
        return types.InputRequiredResult(input_requests={key: types.ElicitRequest(
            params=types.ElicitRequestFormParams(message="Choose a color", requested_schema={
                "type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"],
            }),
        ) for key in ("choice", "second")[:questions]})
    return server


async def pending_question(chat):
    async with asyncio.timeout(5):
        while True:
            pending = await uq.get_all_pending_shared(chat)
            if pending:
                return pending[0]
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_answer_after_execution_budget_still_resumes_real_mcp_call():
    chat = "mcp-wait-" + uuid.uuid4().hex
    async with probe_server(question_server()) as (url, _):
        resource = await discover("human-budget", {
            "transport": "streamable_http", "url": url, "execution_timeout": .5, "transport_timeout": .15,
        }, interactions=_run("unused", chat=chat))
        task = asyncio.create_task(resource.tools[0](after=.25))
        try:
            question = await pending_question(chat)
            await asyncio.sleep(.7)
            assert not task.done(), "Human wait consumed the MCP execution budget"
            reply = await uq.answer_shared(chat, question["request_id"], [{"id": "color", "custom": "blue"}])
            assert reply["ok"]
            result = await asyncio.wait_for(task, 3)
            assert "accept:blue" in str(result.content)
            assert not await uq.get_all_pending_shared(chat)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await resource.close()


@pytest.mark.asyncio
async def test_answer_resumes_remaining_budget_instead_of_resetting_it():
    chat = "mcp-remaining-" + uuid.uuid4().hex
    async with probe_server(question_server()) as (url, _):
        resource = await discover("human-budget", {
            "transport": "streamable_http", "url": url, "execution_timeout": .8,
        }, interactions=_run("unused", chat=chat))
        # Each server phase fits the budget alone; their sum must still time out.
        task = asyncio.create_task(resource.tools[0](before=.5, after=.5))
        try:
            question = await pending_question(chat)
            await asyncio.sleep(1)
            assert not task.done()
            assert (await uq.answer_shared(chat, question["request_id"], [
                {"id": "color", "custom": "blue"},
            ]))["ok"]
            result = await asyncio.wait_for(task, 2)
            assert result.metadata.get("outcome_unknown") is True
            assert "TimeoutError" in str(result.content)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await resource.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_call", [False, True])
async def test_cancellation_during_human_wait_cleans_pending_question(cancel_call):
    chat = "mcp-cancel-" + uuid.uuid4().hex
    async with probe_server(question_server()) as (url, _):
        resource = await discover("human-budget", {
            "transport": "streamable_http", "url": url, "execution_timeout": .5,
        }, interactions=_run("unused", chat=chat))
        task = asyncio.create_task(resource.tools[0]())
        try:
            question = await pending_question(chat)
            await asyncio.sleep(.7)
            assert not task.done()
            if cancel_call:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                assert (await uq.cancel_shared(chat, question["request_id"]))["ok"]
                assert "cancel:" in str((await asyncio.wait_for(task, 2)).content)
            assert not await uq.get_all_pending_shared(chat)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await resource.close()


@asynccontextmanager
async def legacy_server():
    # A real pre-2026 protocol peer: negotiate initialize and issue a callback
    # while tools/call remains outstanding (unlike modern InputRequiredResult).
    messages = asyncio.Queue()
    tool_id = None
    invocation_method = None

    async def stream(request):
        async def events():
            yield "event: endpoint\ndata: /messages/\n\n"
            while True:
                yield "event: message\ndata: " + json.dumps(await messages.get()) + "\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    async def post(request):
        nonlocal tool_id, invocation_method
        body = await request.json()
        method = body.get("method")
        ident = body.get("id")
        result = {}
        if method == "server/discover":
            await messages.put({"jsonrpc": "2.0", "id": ident,
                                "error": {"code": -32601, "message": "Method not found"}})
            return Response(status_code=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                      "serverInfo": {"name": "legacy-human-budget", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "choose", "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "resources/list":
            result = {"resources": [{"uri": "test://color", "name": "color"}]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "color"}]}
        elif method in {"tools/call", "resources/read", "prompts/get"}:
            tool_id = ident
            invocation_method = method
            await messages.put({"jsonrpc": "2.0", "id": "question", "method": "elicitation/create",
                                "params": {"mode": "form", "message": "Choose a color",
                                           "requestedSchema": {"type": "object",
                                               "properties": {"color": {"type": "string"}}, "required": ["color"]}}})
            return Response(status_code=202)
        elif method is None and ident == "question":
            answer = body["result"]
            ident = tool_id
            text = answer["action"] + ":" + answer.get("content", {}).get("color", "")
            if invocation_method == "resources/read":
                result = {"contents": [{"uri": "test://color", "text": text}]}
            elif invocation_method == "prompts/get":
                result = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
            else:
                result = {"content": [{"type": "text", "text": text}]}
        if ident is not None:
            await messages.put({"jsonrpc": "2.0", "id": ident, "result": result})
        return Response(status_code=202)

    app = Starlette(routes=[Route("/sse", stream), Route("/messages/", post, methods=["POST"])])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    http = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(http.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not http.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/sse"
    finally:
        http.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "resource", "prompt"])
async def test_legacy_sse_request_clock_also_excludes_human_wait(kind):
    chat = "mcp-legacy-" + uuid.uuid4().hex
    async with legacy_server() as url:
        resource = await discover("legacy-human-budget", {
            "transport": "sse", "url": url, "execution_timeout": .5,
        }, interactions=_run("unused", chat=chat))
        tool = next(tool for tool in resource.tools if getattr(tool, "kind", "tool") == kind)
        task = asyncio.create_task(tool())
        try:
            question = await pending_question(chat)
            await asyncio.sleep(.7)
            assert not task.done(), "Legacy SDK request timeout interrupted the human wait"
            assert (await uq.answer_shared(chat, question["request_id"], [
                {"id": "color", "custom": "blue"},
            ]))["ok"]
            assert "accept:blue" in str((await asyncio.wait_for(task, 3)).content)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await resource.close()


@pytest.mark.asyncio
async def test_overlapping_questions_keep_budget_paused_until_last_answer():
    chat = "mcp-overlap-" + uuid.uuid4().hex
    async with probe_server(question_server()) as (url, _):
        resource = await discover("human-budget", {
            "transport": "streamable_http", "url": url, "execution_timeout": .5,
        }, interactions=_run("unused", chat=chat))
        task = asyncio.create_task(resource.tools[0](questions=2))
        try:
            async with asyncio.timeout(5):
                while len(pending := await uq.get_all_pending_shared(chat)) < 2:
                    await asyncio.sleep(.01)
            for question in pending:
                await asyncio.sleep(.7)
                assert not task.done()
                assert (await uq.answer_shared(chat, question["request_id"], [
                    {"id": "color", "custom": "blue"},
                ]))["ok"]
            assert "accept:blue" in str((await asyncio.wait_for(task, 3)).content)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await resource.close()


@pytest.mark.asyncio
async def test_waiting_call_does_not_pause_another_invocations_budget():
    chat = "mcp-isolated-" + uuid.uuid4().hex
    async with probe_server(question_server()) as (url, _):
        cfg = {"transport": "streamable_http", "url": url, "execution_timeout": .5}
        waiting = await discover("human-budget", cfg, interactions=_run("unused", chat=chat))
        working = await discover("human-budget", cfg, interactions=_run("unused", chat=chat + "-other"))
        task = asyncio.create_task(waiting.tools[0]())
        try:
            question = await pending_question(chat)
            result = await asyncio.wait_for(working.tools[0](before=1), 2)
            assert result.metadata.get("outcome_unknown") is True
            assert not task.done()
            assert (await uq.answer_shared(chat, question["request_id"], [
                {"id": "color", "custom": "blue"},
            ]))["ok"]
            assert "accept:blue" in str((await asyncio.wait_for(task, 3)).content)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await waiting.close()
            await working.close()
