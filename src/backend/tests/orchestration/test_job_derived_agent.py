"""Workflow forks keep the main-agent surface and independent run state."""
from core.llm.runtime import streaming as streaming_module

import asyncio
import json
from types import SimpleNamespace

import core.jobs.agent as agent
import core.jobs.files as files
import core.jobs.runtime as jobs_runtime
import pytest
from core.llm.factory.request import AgentRequest


def test_snapshot_keeps_parent_and_deferred_capabilities_without_run_identity():
    from core.jobs.agent import snapshot_parent_runtime

    request = AgentRequest(
        enabled_skill_ids=["research"],
        enabled_kb_ids=["kb"],
        visible_subagents=[{"agent_id": "builtin.worker"}],
        chat_mode="thinking",
        run_id="parent-run",
        journal_owner="parent-owner",
        project_ctx={"project_id": "project"},
        workspace_id="team",
        approval_mode="ask",
    )
    deferred = SimpleNamespace(
        directory=[
            SimpleNamespace(
                skill_ids=["plugin-skill"], mcp_ids=["plugin-mcp"], bound_mcp_ids=["bound-mcp"]
            )
        ]
    )
    runtime = snapshot_parent_runtime(
        request,
        skill_ids=["research"],
        mcp_ids=["search"],
        sandbox_session_id="sandbox",
        progressive=deferred,
    )
    runtime = json.loads(json.dumps(runtime))
    assert runtime["enabled_skill_ids"] == ["research", "plugin-skill"]
    assert runtime["enabled_mcp_ids"] == ["search", "plugin-mcp", "bound-mcp"]
    assert runtime["enabled_kb_ids"] == ["kb"]
    assert runtime["subagent_ids"] == ["builtin.worker"]
    assert runtime["sandbox_session_id"] == "sandbox"
    assert runtime["chat_mode"] == "thinking"
    assert runtime["workspace_id"] == "team"
    assert "run_id" not in runtime and "journal_owner" not in runtime


def test_derived_executor_uses_main_factory_and_independent_prompt(monkeypatch):
    import core.jobs.agent as jobs
    import core.llm.factory as factory
    import core.llm.mcp.manager as mcp
    import core.llm.runtime.streaming as streaming

    calls, prompts, closed = [], [], []

    async def construct(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(), ["client"]

    async def close(clients):
        closed.extend(clients)

    class Stream:
        def __init__(self, *_args):
            pass

        async def stream(self, messages, context):
            prompts.append((messages, context))
            yield "text_delta", "done"

    monkeypatch.setattr(factory, "create_agent_executor", construct)
    monkeypatch.setattr(mcp, "close_clients", close)
    monkeypatch.setattr(streaming_module, 'StreamingAgent', Stream)
    monkeypatch.setattr(agent, "resolve_subagents", lambda *_args: [{"agent_id": "builtin.worker"}])
    runtime = {
        "enabled_skill_ids": ["skill"],
        "enabled_mcp_ids": ["search"],
        "enabled_kb_ids": ["kb"],
        "sandbox_session_id": "sandbox",
        "chat_mode": "thinking",
        "workspace_id": "team",
    }
    result = asyncio.run(
        jobs.run_derived_agent(
            "task",
            parent_runtime=runtime,
            user_id="user",
            chat_id="chat",
            run_id="child-run",
            journal_owner="owner",
            model_name="model",
            model_provider_id="provider",
        )
    )
    assert result == ("done", False)
    cfg = calls[0]
    assert cfg["enabled_skill_ids"] == ["skill"]
    assert cfg["enabled_kb_ids"] == ["kb"]
    assert cfg["visible_subagents"] == [{"agent_id": "builtin.worker"}]
    assert cfg["approval_mode"] == "full"
    assert cfg["derived_main"] is True
    assert cfg["sandbox_session_id"] == "sandbox"
    assert cfg["chat_mode"] == "thinking"
    assert cfg["run_id"] == "child-run"
    assert cfg["isolated"] is True and cfg["top_level_chat"] is False
    assert cfg.get("workflow_mode", False) is False
    assert "max_iters" not in cfg and "read_only" not in cfg and "disable_tools" not in cfg
    assert prompts[0][0] == [{"role": "user", "content": "task"}]
    assert closed == ["client"]


def test_child_executor_inherits_full_approval(monkeypatch):
    import core.capabilities.agents.builtin as builtins
    import core.llm.factory as factory
    from agentscope.message import AssistantMsg
    from core.llm.subagents import tool as subagent_tool

    captured = []

    class Child:
        state = SimpleNamespace(context=[])

        async def reply(self, **_kwargs):
            return AssistantMsg(name="child", content="done")

    async def construct(**kwargs):
        captured.append(kwargs)
        return Child(), []

    monkeypatch.setattr(factory, "create_agent_executor", construct)
    monkeypatch.setattr(builtins, "build_builtin_runtime_profile", lambda *_args: SimpleNamespace())
    out = subagent_tool._run_subagent_in_thread(
        "builtin.worker",
        "worker",
        "task",
        "",
        "user",
        parent_runtime={"approval_mode": "full", "sandbox_session_id": "sandbox"},
    )
    assert out[0] is True
    assert captured[0]["approval_mode"] == "full"


@pytest.mark.asyncio
async def test_job_fence_cancels_the_agent_and_waits_for_cleanup():
    from core.jobs.agent import run_with_job_fence

    started, stopped = asyncio.Event(), asyncio.Event()
    checks = 0

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            stopped.set()

    def validate():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("callback revoked")

    with pytest.raises(RuntimeError, match="callback revoked"):
        await run_with_job_fence(work(), validate, interval=0.01)
    assert started.is_set() and stopped.is_set()


def test_missing_parent_config_does_not_fall_back_to_light_executor():
    from core.jobs.agent import run_derived_agent

    with pytest.raises(ValueError, match="重新提交"):
        asyncio.run(
            run_derived_agent(
                "task",
                parent_runtime=None,
                user_id="user",
                chat_id="chat",
                run_id="run",
                journal_owner="owner",
                model_name=None,
                model_provider_id=None,
            )
        )


def test_real_main_factory_delegates_from_workflow_fork(tmp_path):
    """Real factory and child thread, with a local scripted model and isolated DB."""
    import os
    import subprocess
    import sys

    script = """
import asyncio, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import core.db.models
from core.db.engine import Base, engine, SessionLocal
from core.models.repository import create_provider, assign_role
from core.jobs.agent import run_derived_agent
Base.metadata.create_all(engine)
requests = []
# Inspect real permission services for both factory-created roles.
from features import register_product_tools, register_run_capabilities
register_product_tools()
register_run_capabilities()
import core.llm.factory as factory
from agentscope.message import ToolCallBlock
from core.llm.tool_permissions import ToolPermissionSpec, PermissionIntent, DOMAIN_APPROVAL
original_factory = factory.create_agent_executor
checked = []
async def checked_factory(**kwargs):
    agent, clients = await original_factory(**kwargs)
    if kwargs.get("derived_main"):
        assert agent.react_config.max_iters > 10, agent.react_config.max_iters
    service = agent.toolkit._tool_permission_service
    service.registry.register("approval_probe", ToolPermissionSpec("probe", lambda *_: (
        PermissionIntent(domain=DOMAIN_APPROVAL, action="delete", target="test-only", summary="probe"),
    )), source="test")
    outcome = await service.authorize(ToolCallBlock(id="probe", name="approval_probe", input="{}"))
    assert outcome.proceed and "approved_by_preset:full" in outcome.ticket.reasons
    checked.append("child" if kwargs.get("user_agent") else "main")
    return agent, clients
factory.create_agent_executor = checked_factory
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests.append(payload)
        names = {t['function']['name'] for t in payload.get('tools', [])}
        delegated = any(m['role'] == 'tool' for m in payload['messages'])
        if 'call_subagent' in names and not delegated:
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'delegate-1',
                'type': 'function', 'function': {'name': 'call_subagent',
                'arguments': json.dumps({'agent_id': 'builtin.worker', 'task': 'Reply CHILD_OK'})}}]}
            finish = 'tool_calls'
        else:
            delta, finish = {'role': 'assistant', 'content': 'PARENT_OK' if delegated else 'CHILD_OK'}, 'stop'
        chunk = {'id': 'completion', 'object': 'chat.completion.chunk', 'created': 1,
                 'model': payload['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
        end = dict(chunk, choices=[{'index': 0, 'delta': {}, 'finish_reason': finish}])
        data = ('data: '+json.dumps(chunk)+'\\n\\n'+'data: '+json.dumps(end)+'\\n\\n'+'data: [DONE]\\n\\n').encode()
        self.send_response(200); self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
with SessionLocal() as db:
    provider = create_provider(db, display_name='local-test', provider_type='chat',
        base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='test-only', model_name='local-test',
        extra_config={'context_length':32768,'api_protocol':'chat_completions'})
    provider_id = provider.provider_id
    assign_role(db,'main_agent',provider_id)
async def main():
    from core.runs.journal import durable_run_binding
    async with durable_run_binding(user_id="test-user", chat_id="test-chat",
        kind="internal_job_agent", external_id="test", request_payload={}) as binding:
        result = await run_derived_agent('Delegate to the worker, then report.',
        parent_runtime={'enabled_skill_ids':[], 'enabled_mcp_ids':[], 'enabled_kb_ids':[],
                        'subagent_ids':['builtin.worker'], 'chat_mode':'fast', 'sandbox_session_id':'sandbox'},
        user_id='test-user',chat_id=binding.chat_id,run_id=binding.run_id,journal_owner=binding.owner,
        model_name='local-test',model_provider_id=provider_id)
    assert result == ('PARENT_OK', False), result
asyncio.run(main())
assert checked == ["main", "child"], checked
assert len(requests) == 3, len(requests)
parent_tools = {t['function']['name'] for t in requests[0]['tools']}
assert 'call_subagent' in parent_tools and 'run_job' not in parent_tools
assert 'ask_user_question' not in parent_tools
child_tools = {t['function']['name'] for t in requests[1].get('tools',[])}
assert 'call_subagent' not in child_tools
assert any('CHILD_OK' in str(m) for m in requests[2]['messages'] if m['role']=='tool')
server.shutdown()
"""
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite:///{tmp_path / 'agent.db'}",
        PYTHONPATH="src/backend",
        PLUGIN_PROGRESSIVE_LOADING="false",
        SANDBOX_TOOLS_ENABLED="false",
        MEM0_ENABLED="false",
        TEST_DATABASE_URL=f"sqlite:///{tmp_path / 'tests.db'}",
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


@pytest.fixture
def submitted_job(monkeypatch):
    """Real registration, persisted start parameters and authenticated HTTP callback."""
    import base64
    from contextlib import asynccontextmanager

    import core.db.engine as db_engine
    import core.jobs.files as files
    import core.jobs.runtime as job_runtime
    from api.routes.v1 import internal_jobs, job_callback_session
    from core.db.engine import Base
    from core.jobs.agent import snapshot_parent_runtime
    from core.llm.tools.job_tool import register_run_job
    from core.runs import journal as run_journal
    from core.jobs.store import JobService
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    for module in (db_engine, internal_jobs, job_callback_session):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    internal_jobs._job_gates.clear()
    ids = []

    async def start(**kwargs):
        with sessions() as db:
            row = JobService(db).create(
                user_id=kwargs["user_id"],
                chat_id=kwargs["chat_id"],
                name=kwargs["name"],
                script_path=kwargs["script_path"],
                script_text=kwargs["script_text"],
                sandbox_session_id=kwargs["session_id"],
                budget=kwargs["budget"],
                start_params=kwargs["start_params"],
            )
            ids.append((row.job_id, row.extra_data["token"]))
        return ids[-1][0]

    async def bash(*_args, **_kwargs):
        return 0, base64.b64encode(b"print(1)").decode(), ""

    @asynccontextmanager
    async def binding(**_kwargs):
        yield SimpleNamespace(chat_id="chat", run_id="run", owner="owner")

    monkeypatch.setattr(jobs_runtime, "start_job", start)
    monkeypatch.setattr(files, "_sbx_bash", bash)
    monkeypatch.setattr(run_journal, "durable_run_binding", binding)

    class Toolkit:
        def append(self, tool):
            fn = tool._func
            self.call = fn

    runtime = snapshot_parent_runtime(
        AgentRequest(
            chat_mode="thinking",
            enabled_kb_ids=["kb"],
            visible_subagents=[{"agent_id": "builtin.worker"}],
        ),
        skill_ids=["skill"],
        mcp_ids=["search"],
        sandbox_session_id="sandbox",
    )
    toolkit = Toolkit()
    register_run_job(
        toolkit,
        user_id="user",
        chat_id="chat",
        sandbox_session_id="sandbox",
        allowed_tools=["search"],
        parent_runtime=runtime,
    )
    asyncio.run(toolkit.call(script_path="/workspace/job.py"))
    app = FastAPI()
    app.include_router(internal_jobs.router)
    with TestClient(app) as client:
        yield client, ids[0], runtime, sessions
    engine.dispose()


@pytest.mark.parametrize(
    "output, infra_failure, status",
    [
        ('{"ok": true}', False, 200),
        ("not json", False, 422),
        ("partial output", True, 409),
    ],
)
def test_callback_uses_saved_main_snapshot_and_never_repeats_actions(
    submitted_job, monkeypatch, output, infra_failure, status
):
    from api.routes.v1 import internal_jobs
    from core.jobs.store import JobService

    client, (job_id, token), runtime, sessions = submitted_job
    calls = []

    async def execute(prompt, **kwargs):
        calls.append(kwargs)
        return output, infra_failure

    monkeypatch.setattr(internal_jobs, "run_derived_agent", execute)
    response = client.post(
        f"/v1/internal/jobs/{job_id}/agent",
        headers={"X-Job-Token": token},
        json={"prompt": "perform task", "schema": {"type": "object"}, "max_attempts": 4},
    )
    assert response.status_code == status, response.text
    assert len(calls) == 1
    assert calls[0]["parent_runtime"] == json.loads(json.dumps(runtime))
    assert calls[0]["tools"] is None
    with sessions() as db:
        assert JobService(db).get(job_id).usage["calls"] == 1
    denied = client.post(
        f"/v1/internal/jobs/{job_id}/agent",
        headers={"X-Job-Token": "invalid"},
        json={"prompt": "task"},
    )
    assert denied.status_code == 403 and len(calls) == 1


def test_sdk_does_not_repeat_an_agent_after_network_result_is_unknown():
    from core.jobs.sdk import SDK_SOURCE

    ns = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    calls = []

    def urlopen(*_args, **_kwargs):
        calls.append(1)
        raise OSError("lost response")

    ns["BASE"] = "http://callback.test"
    ns["JOB_ID"] = "job"
    ns["urllib"] = SimpleNamespace(
        request=SimpleNamespace(
            urlopen=urlopen, Request=lambda *_a, **_k: SimpleNamespace(add_header=lambda *_a: None)
        ),
        error=SimpleNamespace(HTTPError=type("HttpError", (Exception,), {})),
    )
    with pytest.raises(ns["JobError"], match="result unknown"):
        ns["agent"]("write once")
    assert calls == [1]
