"""A desktop gateway reissues native MCP proofs with its authenticated identity."""
from types import SimpleNamespace
import pytest
from core.llm.mcp import invocation as mcp_invocation, client as mcp_pool
from features.desktop.capability import mcp as gateway

@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_gateway_replaces_device_proof_only_for_trusted_native_target(monkeypatch, native):
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "synthetic-cloud-signing-key")
    from core.config.mcp_config import mcp_http_url as _mcp_http_url
    url = _mcp_http_url("browser_runtime") if native else "https://third-party.example/mcp"
    captured = {}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        async def call_tool(self, name, arguments):
            from mcp.types import CallToolResult
            return CallToolResult(content=[])
    def make_client(sid, config, **kwargs):
        captured.update(config["headers"])
        return Client()
    monkeypatch.setattr(mcp_pool, "make_client", make_client)
    monkeypatch.setattr(gateway, "guard_capability_content", lambda uid, data, **kw: data)
    await gateway.invoke_gateway_tool(
        {"user_id": "cloud-user", "server_id": "browser",
         "target": {"url": url}, "tool": {"name": "browser_open", "inputSchema": {"type": "object"}}},
        {}, {"x-current-user-id": "spoofed-user", "x-chat-id": "authorized-chat",
             "x-hugagent-invocation": "untrusted-device-proof"},
    )
    normalized = {k.lower(): v for k, v in captured.items()}
    assert normalized["x-current-user-id"] == "cloud-user"
    if native:
        normalized[mcp_invocation.HEADER] = normalized[mcp_invocation.HEADER.lower()]
        claims = mcp_invocation.verify(normalized, "browser_runtime")
        assert claims["user"] == "cloud-user" and claims["chat"] == "authorized-chat"
    else:
        assert mcp_invocation.HEADER.lower() not in normalized
