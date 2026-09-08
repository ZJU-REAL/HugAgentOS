"""A lost cloud write response must survive Toolkit error conversion."""

from types import SimpleNamespace
import httpx
import pytest
from agentscope.message import ToolCallBlock
from agentscope.tool import Toolkit
from core.llm.middlewares import AgentRuntimeState, ToolEffectMiddleware
from core.llm.mcp_pool import make_client
from core.db.models import ChatRun, ToolEffectLedger
from core.services.run_journal import RunJournal
from core.services.tool_effect_ledger import (
    ToolOutcomeUnknown,
    ToolEffectJournal,
    recover_incomplete_tool_effects,
)
from tests.llm.test_manifest_mcp_client import _config
from tests.orchestration.test_tool_effect_ledger import effect_env


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "502", "504", "422", "invalid_json"])
async def test_ambiguous_cloud_write_stops_run_without_committed_error_or_replay(
    effect_env, monkeypatch, failure
):
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    sessions, make_run = effect_env
    make_run("cloud-write")
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("connection lost after external write", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"lost-result")
        return httpx.Response(int(failure), json={"detail": "upstream result unavailable"})

    client = make_client("search", _config(httpx.MockTransport(upstream)), is_stateful=False)
    toolkit = Toolkit(mcps=[client])
    state = AgentRuntimeState(run_id="cloud-write", journal_owner="worker")
    middleware = ToolEffectMiddleware(session_factory=sessions)

    async def invoke(**kwargs):
        async for item in toolkit.call_tool(kwargs["tool_call"], state):
            yield item

    with pytest.raises(ToolOutcomeUnknown):
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(state=state),
                {
                    "tool_call": ToolCallBlock(
                        id="cloud-call", name="search", input='{"query":"write"}'
                    )
                },
                invoke,
            )
        ]
    with sessions() as db:
        assert [e.event_type for e in db.query(ToolEffectLedger).all()] == ["intent"]
    assert RunJournal(sessions).needs_attention(
        "cloud-write", owner="worker", reason="cloud write result unknown"
    )
    await recover_incomplete_tool_effects(journal=ToolEffectJournal(sessions))
    with sessions() as db:
        assert db.get(ChatRun, "cloud-write").status == "needs_attention"
        assert [
            e.event_type for e in db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id)
        ] == ["intent", "recovery_claim", "unknown_outcome"]
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_rejected_write_does_not_claim_an_unknown_effect(monkeypatch, status):
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)

    async def rejected(_request):
        return httpx.Response(status, json={"detail": "not authorized or stale schema"})

    client = make_client("search", _config(httpx.MockTransport(rejected)), is_stateful=False)
    with pytest.raises(RuntimeError):
        await (await client.get_tool("search"))(query="write")


@pytest.mark.asyncio
async def test_read_only_timeout_remains_an_ordinary_recoverable_error(monkeypatch):
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)

    async def timeout(request):
        raise httpx.ReadTimeout("lost read response", request=request)

    config = _config(httpx.MockTransport(timeout))
    config["manifest_tools"][0]["annotations"] = {"readOnlyHint": True}
    client = make_client("search", config, is_stateful=False)
    with pytest.raises(RuntimeError, match="调用超时"):
        await (await client.get_tool("search"))(query="read")
