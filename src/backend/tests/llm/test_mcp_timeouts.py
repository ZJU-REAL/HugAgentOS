from __future__ import annotations

from types import SimpleNamespace


def test_kb_client_gets_tighter_execution_and_transport_deadlines(monkeypatch):
    from core.llm.mcp.client import make_client

    monkeypatch.delenv("KB_MCP_EXECUTION_TIMEOUT_SECONDS", raising=False)
    client = make_client(
        "retrieve_dataset_content",
        {"transport": "streamable_http", "url": "http://mcp:9100/mcp/"},
    )

    assert client.read_timeout_seconds == 75.0
    from core.llm.mcp.client import _http_transport_timeout
    assert _http_transport_timeout("retrieve_dataset_content", {}, client.read_timeout_seconds) == 80.0


def test_mcp_timeout_config_overrides_defaults():
    from core.llm.mcp.client import make_client

    client = make_client(
        "custom",
        {
            "transport": "streamable_http",
            "url": "http://custom/mcp",
            "execution_timeout": 240,
            "transport_timeout": 250,
        },
    )

    assert client.read_timeout_seconds == 240.0
    from core.llm.mcp.client import _http_transport_timeout
    assert _http_transport_timeout("custom", {"transport_timeout": 250}, client.read_timeout_seconds) == 250.0


def test_stdio_client_also_has_a_hard_execution_deadline(monkeypatch):
    from core.llm.mcp.client import make_client

    monkeypatch.setenv("MCP_TOOL_EXECUTION_TIMEOUT_SECONDS", "90")
    client = make_client(
        "stdio-tool",
        {"transport": "stdio", "command": "python", "args": []},
    )

    assert client.read_timeout_seconds == 90.0


def test_database_extra_config_exposes_timeout_overrides(monkeypatch):
    from core.capabilities.mcp.service import McpServerConfigService

    row = SimpleNamespace(
        transport="streamable_http",
        url="http://remote/mcp",
        headers={},
        extra_config={"execution_timeout": 180, "transport_timeout": 190},
        # Provenance columns every AdminMcpServer row carries (read for logical dedup).
        source_plugin=None,
        owner_user_id=None,
    )
    service = McpServerConfigService()
    monkeypatch.setattr(service, "_build_env", lambda _row: {})

    config = service._row_to_config(row)
    assert config["execution_timeout"] == 180
    assert config["transport_timeout"] == 190
