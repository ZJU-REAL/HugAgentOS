"""Native MCP cleanup must run even when stopping the producer fails."""
import asyncio
from types import SimpleNamespace
import pytest
from core.llm.runtime.streaming import StreamingAgent

@pytest.mark.asyncio
async def test_shutdown_releases_all_resources_when_producer_cancelled():
    closed = []
    class Resource:
        async def close(self):
            closed.append(self)
    async def stop():
        raise asyncio.CancelledError()
    clients = [Resource(), Resource()]
    agent = SimpleNamespace(stop_producer=stop, mcp_clients=clients)
    with pytest.raises(asyncio.CancelledError):
        await StreamingAgent.shutdown(agent)
    assert closed == clients
    assert agent.mcp_clients == []
