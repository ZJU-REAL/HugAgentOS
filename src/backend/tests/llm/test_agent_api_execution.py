"""Real agent factory and model-wire integration for resource-scoped keys."""
import os
import subprocess
import sys


def test_scoped_factory_executes_bound_model_with_isolated_tools_and_offloader(tmp_path):
    # A separate process prevents cached model settings from touching the developer
    # database. All model traffic terminates at this in-process HTTP fixture.
    script = r"""
import asyncio
import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import core.db.models
import core.sandbox
import core.sandbox.factory
from agentscope.message import UserMsg
from core.db.engine import Base, engine, SessionLocal
from core.db.models import UserAgent, UserShadow, ProfileMemory
from core.db.model_repository import create_provider, assign_role
from core.evolution import runtime_binding
from core.llm.agent_factory import create_agent_executor

Base.metadata.create_all(engine)
requests = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(payload)
        chunk = {
            "id": "scoped-completion", "object": "chat.completion.chunk", "created": 1,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "scoped done"}, "finish_reason": None}],
        }
        end = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
        body = ("data: " + json.dumps(chunk) + "\n\n"
                + "data: " + json.dumps(end) + "\n\n"
                + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
with SessionLocal() as db:
    def add_model(name):
        return create_provider(
            db, display_name=name, provider_type="chat",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-only", model_name=name,
            extra_config={"context_length": 32768, "api_protocol": "chat_completions"},
        ).provider_id
    main_id = add_model("owner-main")
    child_id = add_model("bound-api-agent")
    assign_role(db, "main_agent", main_id)
    db.add(UserShadow(user_id="owner", username="Owner"))
    db.add(ProfileMemory(user_id="owner", workspace_id="default",
                         content_md="OWNER_PRIVATE_MEMORY_SENTINEL"))
    profile = UserAgent(
        agent_id="ua_api", owner_type="user", user_id="owner", name="Bound API Agent",
        system_prompt="BOUND_AGENT_PROMPT_SENTINEL. Reply scoped done.",
        model_provider_id=child_id, mcp_server_ids=[], skill_ids=[], plugin_ids=[], kb_ids=[],
        timeout=15, max_iters=2,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    db.expunge(profile)

scope = {
    "version": 1, "owner_user_id": "owner", "api_key_id": "ak_test",
    "agent_id": "ua_api", "chat_id": "api_chat", "sandbox_user_id": "api_ak_test",
    "sandbox_session_id": "api_" + hashlib.sha256(b"ak_test:api_chat").hexdigest()[:40],
}
class Sandbox:
    name = "opensandbox"
    runs_on_host = False
    def __init__(self):
        self.writes = []
    async def put_file(self, session_id, path, content, user_id):
        self.writes.append((session_id, path, content, user_id))
sandbox = Sandbox()

original_connect = socket.socket.connect
def local_model_only(sock, address):
    if isinstance(address, tuple):
        assert address[:2] == ("127.0.0.1", server.server_port), f"Unexpected network request: {address!r}"
    return original_connect(sock, address)

async def run():
    with patch.object(core.sandbox, "get_sandbox_provider", return_value=sandbox), patch.object(
        core.sandbox.factory, "get_sandbox_provider", return_value=sandbox
    ), patch.object(runtime_binding, "bind_runtime_assets", wraps=runtime_binding.bind_runtime_assets) as bind:
        agent, clients = await create_agent_executor(
            user_agent=profile, current_user_id="owner", chat_id="api_chat",
            agent_api_scope=scope, memory_enabled=True, model_provider_id=main_id,
            chat_mode="fast", isolated=True, max_iters=2,
            project_ctx={"project_name": "OWNER_PRIVATE_PROJECT_SENTINEL"},
            visible_subagents=[{"agent_id": "ua_other", "name": "OWNER_OTHER_AGENT_SENTINEL"}],
        )
        try:
            assert bind.call_args.kwargs["memory_enabled"] is False
            assert agent.state.model_name == "bound-api-agent"
            assert agent.state.model_provider_id == child_id
            assert agent.state.model_pinned is True
            policies = {
                type(policy).__name__
                for adapter in agent._reply_middlewares
                for policy in getattr(adapter, "legacy_middlewares", ())
            }
            assert "FileContextMiddleware" in policies
            assert "FinishPinGuardMiddleware" not in policies, "automatic pinning bypasses scoped artifact checks"
            schemas = {item["function"]["name"]: item["function"]
                       for item in agent._jx_compaction_tool_schemas}
            assert {"Bash", "write_stdin", "sandbox_get_artifact", "pin_to_workspace", "read_artifact"} <= schemas.keys()
            assert not {"read_chat", "list_related_chats", "load_plugin", "call_subagent"} & schemas.keys()
            assert not any("myspace" in name.lower() for name in schemas)
            assert "API" in schemas["Bash"]["description"]
            assert "file_paths" not in schemas["pin_to_workspace"]["parameters"]["properties"]
            assert "file_ids" in schemas["pin_to_workspace"]["parameters"]["properties"]

            # This is the offloader constructed by the real factory, not a
            # separately configured fake. Overflow must use the same API principal.
            assert agent.offloader is not None
            overflow = "large scoped tool output " * 5000
            path = await agent.offloader.offload_tool_result(
                "framework-session", SimpleNamespace(output=overflow),
            )
            assert len(sandbox.writes) == 1
            session_id, saved_path, content, user_id = sandbox.writes[0]
            assert (session_id, user_id) == (scope["sandbox_session_id"], scope["sandbox_user_id"])
            assert saved_path == path and "/.offload/" in path
            assert content.decode() == overflow

            result = await agent.reply(UserMsg(name="user", content="Reply scoped done"))
            assert result.get_text_content() == "scoped done"
            assert len(requests) == 1
            wire = requests[0]
            assert wire["model"] == "bound-api-agent"
            assert wire["chat_template_kwargs"]["enable_thinking"] is False
            prompt = json.dumps(wire["messages"], ensure_ascii=False)
            assert "BOUND_AGENT_PROMPT_SENTINEL" in prompt
            for private in ("OWNER_PRIVATE_MEMORY_SENTINEL", "OWNER_PRIVATE_PROJECT_SENTINEL",
                            "OWNER_OTHER_AGENT_SENTINEL"):
                assert private not in prompt
            sent_tools = {tool["function"]["name"] for tool in wire["tools"]}
            assert sent_tools == set(schemas)
        finally:
            for client in clients:
                await client.close()

try:
    with patch.object(socket.socket, "connect", local_model_only):
        asyncio.run(run())
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    engine.dispose()
"""
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite:///{tmp_path / 'scoped-execution.db'}",
        REDIS_URL="",
        SANDBOX_TOOLS_ENABLED="true",
        CODE_CAPABILITY_ENABLED="false",
        JX_CAPABILITIES_ENABLED="false",
        HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="",
        http_proxy="", https_proxy="", all_proxy="",
        NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    diagnostics = "\n".join(line for line in output.splitlines() if any(kind in line for kind in ("Unexpected network", "AssertionError", "TypeError", "connect.failed", "Exception:")))
    assert result.returncode == 0, diagnostics + "\n" + output[-4000:]
