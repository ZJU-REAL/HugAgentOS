import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.engine import Base
from mcp_servers.automation_task_mcp import impl


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    import core.db.engine as engine_module
    engine = create_engine(f"sqlite:///{tmp_path / 'cloud.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", factory)
    yield factory
    engine.dispose()


def test_remote_create_retry_returns_one_task_and_rejects_changed_arguments(sessions):
    from core.services.automation_remote_effect import bind_remote_effect
    args = {"cron_expression": "0 18 * * *", "prompt": "daily report", "name": "daily"}
    effect = bind_remote_effect("user-a", "automation", "create_scheduled_task", "local-effect", args)
    first = impl.create_task(user_id="user-a", tool_effect_id=effect, **args)
    assert first["ok"] is True
    # Simulate losing the first HTTP response and resubmitting the same operation.
    again = bind_remote_effect("user-a", "automation", "create_scheduled_task", "local-effect", args)
    assert impl.create_task(user_id="user-a", tool_effect_id=again, **args) == first
    assert impl.list_tasks(user_id="user-a")["count"] == 1
    with pytest.raises(ValueError, match="different"):
        bind_remote_effect("user-a", "automation", "create_scheduled_task", "local-effect", {**args, "prompt": "changed"})
    denied = impl.create_task(user_id="user-b", tool_effect_id=effect, **args)
    assert denied["ok"] is False
    assert impl.list_tasks(user_id="user-b")["count"] == 0


@pytest.mark.asyncio
async def test_device_gateway_maps_local_intent_before_cloud_mcp(effect_env, tmp_path, monkeypatch):
    import json
    import httpx
    import mcp.types
    from types import SimpleNamespace
    from agentscope.message import ToolCallBlock, ToolResultState, TextBlock
    from agentscope.tool import Toolkit
    from agentscope.tool._response import ToolChunk
    from core.llm.mcp_manager import GatewayMCPTool
    from core.llm.middlewares import AgentRuntimeState, ToolEffectMiddleware
    import core.db.engine as engine_module
    from core.services.desktop_capability import invoke_gateway_tool

    local, make_run = effect_env
    make_run("gateway-run")
    cloud_engine = create_engine(f"sqlite:///{tmp_path / 'remote.sqlite'}")
    Base.metadata.create_all(cloud_engine)
    cloud = sessionmaker(bind=cloud_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", local)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    raw = {"name": "create_scheduled_task", "inputSchema": {"type": "object", "properties": {}}}

    class CloudClient:
        execution_timeout = 10
        async def get_tool(self, name):
            async def invoke(**args):
                result = impl.create_task(user_id="user-1", **args)
                return ToolChunk(content=[TextBlock(text=json.dumps(result))],
                                 state=ToolResultState.SUCCESS if result["ok"] else ToolResultState.ERROR)
            return invoke
    monkeypatch.setattr("core.llm.mcp_pool.make_client", lambda *a, **kw: CloudClient())

    received = []
    async def endpoint(request):
        body = json.loads(request.content)
        received.append(body["arguments"]["tool_effect_id"])
        with monkeypatch.context() as patch:
            patch.setattr(engine_module, "SessionLocal", cloud)
            result = await invoke_gateway_tool(
                {"user_id": "user-1", "server_id": "automation", "tool": raw, "target": {}},
                body["arguments"], {},
            )
        return httpx.Response(200, json={"code": 200, "data": result})

    tool = GatewayMCPTool(mcp_name="automation", tool=mcp.types.Tool.model_validate(raw),
        invoke_url="https://cloud.invalid/api/v1/desktop/capability/gateway/automation/call",
        schema_hash="a"*64, headers={}, timeout=10, transport=httpx.MockTransport(endpoint))
    toolkit = Toolkit(tools=[tool])
    state = AgentRuntimeState(run_id="gateway-run", journal_owner="worker", user_id="user-1", chat_id="chat-1")
    middleware = ToolEffectMiddleware(session_factory=local)
    async def invoke(**kwargs):
        async for item in toolkit.call_tool(kwargs["tool_call"], state):
            yield item
    request = {"tool_call": ToolCallBlock(id="gateway-call", name=tool.name,
        input=json.dumps({"cron_expression": "0 18 * * *", "prompt": "report"}))}
    first = [item async for item in middleware.on_acting(SimpleNamespace(state=state), request, invoke)]
    assert first[-1].state == ToolResultState.SUCCESS
    second = [item async for item in middleware.on_acting(SimpleNamespace(state=state), request, invoke)]
    assert second[-1].state == ToolResultState.SUCCESS
    assert len(received) == 1
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "SessionLocal", cloud)
        assert impl.list_tasks(user_id="user-1")["count"] == 1
    cloud_engine.dispose()


from tests.orchestration.test_tool_effect_ledger import effect_env


@pytest.mark.asyncio
async def test_lost_cloud_response_reconciles_receipt_and_never_guesses(sessions, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from core.services.automation_remote_effect import bind_remote_effect, lookup_remote_receipt
    from orchestration.tool_effect_recovery import _reconcile_cloud_receipt
    from core.capabilities.errors import CloudUnavailable
    args = {"cron_expression": "0 18 * * *", "prompt": "report"}
    effect = bind_remote_effect("user-a", "automation", "create_scheduled_task", "lost", args)
    original = impl.create_task(user_id="user-a", tool_effect_id=effect, **args)
    state = {"token": "fixture", "cloud_base": "https://cloud.invalid"}
    monkeypatch.setattr("core.services.desktop_cloud_bridge.get_state", lambda: state)
    monkeypatch.setattr("core.services.desktop_capability_protocol.token_subject", lambda token: "user-a")
    monkeypatch.setattr("core.services.desktop_cloud_bridge.ensure_current_authorization", lambda *a: None)
    monkeypatch.setattr("core.services.desktop_cloud_bridge.require_current_account", lambda *a: None)
    monkeypatch.setattr("core.services.desktop_cloud_bridge.cloud_headers", lambda *a: {})
    requests = []
    async def endpoint(request):
        import json
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": lookup_remote_receipt(
            "user-a", "automation", body["tool_name"], body["operation_id"])})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(endpoint), **kw))
    intent = SimpleNamespace(tool_name="create_scheduled_task", effect_id="lost")
    remote = {"user_id": "user-a", "url": "https://cloud.invalid/api/v1/desktop/capability/gateway/automation/call", "schema_hash": "a"*64}
    result = await _reconcile_cloud_receipt(intent, remote)
    assert result.outcome == "applied" and result.result == original
    assert len(requests) == 1 and str(requests[0].url).endswith("/receipt")
    assert impl.list_tasks(user_id="user-a")["count"] == 1
    def expired(*args):
        raise CloudUnavailable("expired")
    monkeypatch.setattr("core.services.desktop_cloud_bridge.ensure_current_authorization", expired)
    assert (await _reconcile_cloud_receipt(intent, remote)).outcome == "unknown"
    assert len(requests) == 1
