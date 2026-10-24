"""Built-in MCP requests must not incur an HTTP redirect."""
from core.capabilities.mcp.service import rewrite_builtin_mcp_host
from core.config.mcp_config import mcp_http_url


def test_builtin_canonical_endpoint():
    assert mcp_http_url("web_fetch").endswith("/mcp")
    assert rewrite_builtin_mcp_host("http://mcp:9106/mcp/").endswith("/mcp")


def test_external_paths_and_queries_are_preserved():
    external = "https://example.com/mcp/?tenant=a"
    assert rewrite_builtin_mcp_host(external) == external
    assert rewrite_builtin_mcp_host("http://mcp:9106/custom/?tenant=a").endswith("/custom/?tenant=a")


def test_local_seed_uses_canonical_endpoint(db_session, monkeypatch):
    from core.config.settings import settings
    from core.capabilities.mcp.service import seed_builtin_mcp_servers_if_empty, McpServerConfigService
    from core.db.models import AdminMcpServer

    from dataclasses import replace

    monkeypatch.setattr("core.config.settings.settings",
                        replace(settings, server=replace(settings.server, mcp_host="127.0.0.1")))
    assert seed_builtin_mcp_servers_if_empty(db_session)
    for row in db_session.query(AdminMcpServer).all():
        assert row.url.startswith("http://127.0.0.1:")
        assert row.url.endswith("/mcp")
        assert McpServerConfigService()._row_to_config(row)["url"] == row.url
