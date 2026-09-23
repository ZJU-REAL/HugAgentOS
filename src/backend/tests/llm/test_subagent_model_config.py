"""Subagent model selection through the configuration API and real executor."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from api.deps import require_system_settings
from api.routes.v1 import models as routes
from core.db.engine import get_db
from core.db.model_repository import create_provider
from core.services import model_config
from core.services.model_config import ModelConfigService


@pytest.fixture
def model_api(db_session, monkeypatch):
    service = ModelConfigService()
    monkeypatch.setattr(ModelConfigService, "_instance", service)
    monkeypatch.setattr(model_config, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_system_settings] = lambda: None
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client, service, db_session


def add_provider(db, name="child", provider_type="chat"):
    return create_provider(
        db,
        display_name=name,
        provider_type=provider_type,
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model_name=name,
        extra_config={"context_length": 32768, "api_protocol": "chat_completions"},
    )


def test_admin_can_assign_and_clear_one_shared_subagent_role(model_api):
    client, service, db = model_api
    provider = add_provider(db)
    roles = client.get("/v1/models/roles").json()["data"]
    child_roles = [r for r in roles if r["role_key"].startswith("subagent")]
    assert [r["role_key"] for r in child_roles] == ["subagent"]
    assert child_roles[0]["provider_id"] is None
    assert service.resolve("subagent") is None

    response = client.put("/v1/models/roles/subagent", json={"provider_id": provider.provider_id})
    assert response.status_code == 200
    assert service.resolve("subagent").model_name == "child"
    roles = client.get("/v1/models/roles").json()["data"]
    assert (
        next(r for r in roles if r["role_key"] == "subagent")["provider_id"] == provider.provider_id
    )
    assert client.delete("/v1/models/roles/subagent").status_code == 200
    assert service.resolve("subagent") is None


@pytest.mark.parametrize(
    "scenario",
    [
        "inherit_builtin",
        "inherit_custom",
        "shared_builtin",
        "shared_custom",
        "disabled",
        "cleared",
        "individual",
        "parameter_override",
        "timeout_only",
        "parameter_failover",
        "system_default",
    ],
)
def test_real_subagent_reply_uses_configured_or_inherited_model(tmp_path, scenario):
    import os
    import subprocess
    import sys

    script = r"""
import asyncio
import json
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core.db.models
from agentscope.message import UserMsg
from core.db.engine import Base, engine, SessionLocal
from core.db.model_repository import create_provider, assign_role, unassign_role, update_provider
from core.llm.agent_factory import create_agent_executor
from core.llm.builtin_subagents import build_builtin_runtime_profile, get_builtin_subagent

Base.metadata.create_all(engine)
requests = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(payload)
        if os.environ["TEST_MODEL_SCENARIO"] == "parameter_failover" and payload["model"] == "user-selected":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        chunk = {
            "id": "test-completion", "object": "chat.completion.chunk", "created": 1,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}],
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
threading.Thread(target=server.serve_forever, daemon=True).start()
with SessionLocal() as db:
    def add(name):
        return create_provider(
            db, display_name=name, provider_type="chat",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-only", model_name=name,
            extra_config={"context_length": 32768, "api_protocol": "chat_completions"},
        ).provider_id
    main_id = add("system-default")
    selected_id = add("user-selected")
    shared_id = add("shared-child")
    individual_id = add("individual-child")
    assign_role(db, "main_agent", main_id)
    scenario = os.environ["TEST_MODEL_SCENARIO"]
    if scenario.startswith("shared") or scenario in ("disabled", "cleared"):
        assign_role(db, "subagent", shared_id)
    if scenario == "disabled":
        update_provider(db, shared_id, is_active=False)
    if scenario == "cleared":
        unassign_role(db, "subagent")

async def run():
    profile = build_builtin_runtime_profile(get_builtin_subagent("builtin.worker"))
    if "custom" in scenario or scenario == "individual":
        profile.agent_id = "custom-test-agent"
    if scenario in ("shared_custom", "individual"):
        profile.model_provider_id = individual_id
    if scenario in ("parameter_override", "parameter_failover"):
        profile.temperature = 0.25
        profile.max_tokens = 500
    if scenario == "timeout_only":
        profile.timeout = 7
    agent, clients = await create_agent_executor(
        user_agent=profile, disable_tools=True, isolated=True,
        model_provider_id=None if scenario == "system_default" else selected_id, chat_mode="fast", max_iters=1,
    )
    if scenario.startswith("shared") or scenario == "individual":
        provider_before = agent.state.model_provider_id
        agent.state.apply_request_context({"model_provider_id": selected_id, "model_name": "user-selected", "chat_mode": "fast"}, "Reply done")
        assert agent.state.model_provider_id == provider_before
        assert agent.state.model_name != "user-selected"
    if scenario == "timeout_only":
        primary = getattr(agent.model, "_primary", agent.model)
        assert primary._http_client.timeout.read == 7
    result = await agent.reply(UserMsg(name="user", content="Reply done"))
    assert result.get_text_content() == "done"
    expected = ("shared-child" if scenario.startswith("shared") else
                "individual-child" if scenario == "individual" else
                "system-default" if scenario == "system_default" else "user-selected")
    if scenario == "parameter_failover":
        assert requests[0]["model"] == "user-selected"
        assert requests[-1]["model"] != "user-selected"
    else:
        assert requests[-1]["model"] == expected, requests[-1]["model"]
    assert requests[-1]["chat_template_kwargs"]["enable_thinking"] is False
    if scenario in ("parameter_override", "parameter_failover"):
        assert requests[-1]["temperature"] == 0.25
        assert requests[-1]["max_tokens"] == 500
    assert agent.state.chat_mode == "fast"
    if scenario != "system_default":
        assert agent.state.model_pinned is True
    for client in clients:
        await client.close()

try:
    asyncio.run(run())
    if scenario in ("inherit_builtin", "shared_builtin", "system_default"):
        from core.llm.subagent_tool import _run_subagent_in_thread
        events = []
        ok, output, _, _ = _run_subagent_in_thread(
            "builtin.worker", "worker", "Reply done", "", "",
            parent_runtime={"model_provider_id": None if scenario == "system_default" else selected_id, "chat_mode": "fast"},
            emit=events.append,
        )
        assert ok, output
        assert output == "done"
        expected = "shared-child" if scenario == "shared_builtin" else "system-default" if scenario == "system_default" else "user-selected"
        assert requests[-1]["model"] == expected
        assert requests[-1]["chat_template_kwargs"]["enable_thinking"] is False
        if scenario == "system_default":
            ok, output, _, _ = _run_subagent_in_thread(
                "builtin.worker", "worker", "Reply done", "", "",
                parent_runtime={"chat_mode": "fast"}, emit=events.append)
            assert ok, output
            asyncio.run(run())
finally:
    server.shutdown()
"""
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite:///{tmp_path / 'models.db'}",
        REDIS_URL="",
        SANDBOX_TOOLS_ENABLED="false",
        CODE_CAPABILITY_ENABLED="false",
        JX_CAPABILITIES_ENABLED="false",
        TEST_MODEL_SCENARIO=scenario,
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8000:]


def test_shared_subagent_role_rejects_non_chat_provider(model_api):
    client, service, db = model_api
    provider = add_provider(db, "embedding", "embedding")
    response = client.put("/v1/models/roles/subagent", json={"provider_id": provider.provider_id})
    assert response.status_code == 400
    assert service.resolve("subagent") is None
