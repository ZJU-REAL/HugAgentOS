from __future__ import annotations

import httpx
import pytest

from agentscope.message import ToolResultState
from core.services.desktop_capability_protocol import (
    CapabilityManifestError,
    build_manifest,
    canonical_hash,
    validate_manifest,
)


def _tools() -> list[dict]:
    return [
        {
            "name": "search",
            "description": "Search cloud data",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]


def _config(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    tools = _tools()
    invoke_url = "https://cloud.example/api/v1/desktop/capability/gateway/search/call"
    return {
        "transport": "streamable_http",
        "url": invoke_url,
        "schema_source": "cloud_manifest",
        "gateway_invoke_url": invoke_url,
        "manifest_tools": tools,
        "schema_hash": canonical_hash(tools),
        "headers": {"Authorization": "Bearer capability-token", "X-Chat-Id": "chat-1"},
        "execution_timeout": 30,
        **({"gateway_transport": transport} if transport is not None else {}),
    }


def test_protocol_accepts_only_a_complete_current_dynamic_manifest():
    tools = _tools()
    manifest = build_manifest(
        [
            {
                "server_id": "search",
                "component": "search",
                "tools": tools,
                "schema_hash": canonical_hash(tools),
            }
        ]
    )

    assert validate_manifest(manifest) == manifest
    with pytest.raises(CapabilityManifestError):
        validate_manifest({"version": 1, "servers": []})
    broken = {**manifest, "revision": "0" * 64}
    with pytest.raises(CapabilityManifestError):
        validate_manifest(broken)


@pytest.mark.asyncio
async def test_manifest_client_lists_cloud_schemas_without_network():
    from core.llm.mcp_manager import ManifestMCPClient
    from core.llm.mcp_pool import make_client

    class NetworkMustNotRun(httpx.AsyncBaseTransport):
        async def handle_async_request(self, _request):  # pragma: no cover
            raise AssertionError("schema listing must not perform network I/O")

    client = make_client("search", _config(NetworkMustNotRun()), is_stateful=False)
    assert isinstance(client, ManifestMCPClient)

    tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["search"]
    assert tools[0].input_schema["required"] == ["query"]


def test_incomplete_manifest_config_is_rejected_not_compatibility_routed():
    from core.llm.mcp_pool import make_client, uses_manifest_schema

    config = _config()
    config["manifest_tools"] = ["search"]

    assert uses_manifest_schema(config) is False
    with pytest.raises(ValueError, match="invalid cloud capability manifest"):
        make_client("search", config, is_stateful=False)


@pytest.mark.asyncio
async def test_manifest_tool_calls_json_gateway_with_current_schema_hash():
    from core.llm.mcp_pool import make_client

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": 10000,
                "data": {
                    "content": [{"type": "text", "text": "cloud result"}],
                    "state": "running",
                    "is_last": True,
                    "metadata": {"origin": "cloud"},
                    "id": "chunk-1",
                },
            },
        )

    config = _config(httpx.MockTransport(handler))
    client = make_client("search", config, is_stateful=False)
    tool = (await client.list_tools())[0]

    result = await tool(query="hello")

    assert captured["url"].endswith("/gateway/search/call")
    assert captured["headers"]["authorization"] == "Bearer capability-token"
    assert captured["json"] == {
        "tool_name": "search",
        "arguments": {"query": "hello"},
        "schema_hash": config["schema_hash"],
    }
    assert result.content[0].text == "cloud result"
    assert result.state == ToolResultState.RUNNING
