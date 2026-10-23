"""Full production factory -> HTTP model -> SDK2 server -> final model reply."""
import os
import subprocess
import sys


def test_production_factory_calls_real_mcp_and_returns_final_reply(tmp_path):
    script = r'''
import asyncio, json, socket
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from agentscope.message import UserMsg
from tests.llm.test_mcp_parallel_execution import probe_server
from core.db.engine import Base, engine, SessionLocal
from core.db.models import Project, UserShadow, ChatSession, AdminMcpServer
from core.models.repository import create_provider, assign_role
from core.harness.registry import load_models
from features.installed import INSTALLED_FEATURES
from features import register_product_tools, register_run_capabilities, register_model_credentials
register_product_tools()
register_run_capabilities()
register_model_credentials()
load_models(INSTALLED_FEATURES)
Base.metadata.create_all(engine)
from core.llm.factory import create_agent_executor
from core.llm.mcp.manager import close_clients

async def run():
    model_app = FastAPI()
    requests = []
    @model_app.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        requests.append(body)
        first = len(requests) == 1
        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "probe-call", "type": "function", "function": {"name": "probe", "arguments": '{"item":7}'}}]} if first else {"role": "assistant", "content": "verified completed-7"}
        async def stream():
            yield "data: " + json.dumps({"id":"fixture", "object":"chat.completion.chunk", "created":1, "model":"fixture", "choices":[{"index":0,"delta":delta,"finish_reason":None}]}) + "\n\n"
            yield "data: " + json.dumps({"id":"fixture", "object":"chat.completion.chunk", "created":1, "model":"fixture", "choices":[{"index":0,"delta":{},"finish_reason":"tool_calls" if first else "stop"}], "usage":{"prompt_tokens":100,"completion_tokens":10,"total_tokens":110}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(model_app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        async with probe_server() as (url, calls):
            with SessionLocal() as db:
                model = create_provider(db, display_name="fixture", provider_type="chat", base_url=f"http://127.0.0.1:{port}/v1", api_key="test-only", model_name="fixture", extra_config={"context_length":32768,"api_protocol":"chat_completions"})
                assign_role(db, "main_agent", model.provider_id)
                db.add(UserShadow(user_id="owner", username="Owner"))
                db.add(Project(project_id="e2e", name="E2E", kind="personal", owner_user_id="owner", extra_data={"environment":{"mode":"custom","mcp_ids":["probe-server"]}}))
                db.add(ChatSession(chat_id="e2e-chat", user_id="owner", project_id="e2e", title="E2E"))
                db.add(AdminMcpServer(server_id="probe-server", display_name="Probe", owner_user_id="owner", is_enabled=True, transport="streamable_http", url=url))
                db.commit()
            from core.runs.journal import RunJournal
            journal = RunJournal(SessionLocal)
            journal.accept(run_id="e2e-run", message_id="e2e-msg", chat_id="e2e-chat", user_id="owner", request_payload={"kind":"chat"}, recovery_snapshot={"kind":"chat","worker_args":{}})
            assert journal.claim("e2e-run", owner="e2e-worker", lease_seconds=300)
            agent, resources = await create_agent_executor(current_user_id="owner", chat_id="e2e-chat", project_ctx={"project_id":"e2e"}, enabled_mcp_ids=["probe-server"], max_iters=3)
            agent.state.run_id = "e2e-run"
            agent.state.journal_owner = "e2e-worker"
            try:
                result = await agent.reply(UserMsg(name="user", content="Call probe with item 7 and report the result."))
                assert list(calls) == [7], calls
                assert len(requests) == 2, len(requests)
                assert "completed-7" in json.dumps(requests[1]), requests[1]
                assert "verified completed-7" in str(result), result
                assert len(resources) == 1
            finally:
                await close_clients(resources)
            assert all(r.session is None or r.session._owner.done() for r in resources)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
asyncio.run(run())
engine.dispose()
'''
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'e2e.db'}", REDIS_URL="", JX_CAPABILITIES_ENABLED="false", LOCAL_MODE="false", SANDBOX_TOOLS_ENABLED="false")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-8000:]
