"""A session loads one copy of a marketplace MCP and survives tool-name clashes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.capabilities.mcp import service as mcp_service
from core.llm.factory.tools import mcp_config
from core.llm.factory.tools.tool_surface import unique_mcp_tools


def _use_globals(monkeypatch, *, enabled: bool = True):
    servers = {"sys_products": {"transport": "streamable_http", "market_item": "products", "owner_user_id": None}}

    class FakeMcpService:
        def get_all_servers(self, enabled_only=True):
            return {} if enabled_only and not enabled else dict(servers)

    monkeypatch.setattr(
        mcp_service.McpServerConfigService, "get_instance", classmethod(lambda _cls: FakeMcpService())
    )


def _owned(credential_source=None):
    cfg = {"transport": "streamable_http", "market_item": "products", "owner_user_id": "u1"}
    if credential_source:
        cfg["credential_source"] = credential_source
    return {"mine_products": cfg}


def test_private_copy_on_admin_credentials_gives_way_to_the_system_copy(monkeypatch):
    _use_globals(monkeypatch)
    keys = mcp_config.effective_mcp_server_keys(
        None, None, enabled_mcp_ids=["sys_products", "mine_products"], owned_servers=_owned("admin")
    )
    assert keys == ["sys_products"]
    aliases = mcp_config.market_copy_aliases(keys, owned_servers=_owned("admin"))
    assert mcp_config._required_mcp_server_keys(["mine_products"], keys, aliases) == ["sys_products"]


@pytest.mark.parametrize("credential_source", ["installer", None])
def test_private_copy_on_own_credentials_wins(monkeypatch, credential_source):
    _use_globals(monkeypatch)
    keys = mcp_config.effective_mcp_server_keys(
        None, None, enabled_mcp_ids=["sys_products", "mine_products"], owned_servers=_owned(credential_source)
    )
    assert keys == ["mine_products"]


def test_a_single_selected_copy_is_left_alone(monkeypatch):
    _use_globals(monkeypatch)
    keys = mcp_config.effective_mcp_server_keys(
        None, None, enabled_mcp_ids=["mine_products"], owned_servers=_owned("admin")
    )
    assert keys == ["mine_products"]


def test_disabled_system_copy_never_wins(monkeypatch):
    _use_globals(monkeypatch, enabled=False)
    keys = mcp_config.effective_mcp_server_keys(
        None, None, enabled_mcp_ids=["sys_products", "mine_products"], owned_servers=_owned("admin")
    )
    assert keys == ["mine_products"]


def _resource(name, tools, display_name):
    return SimpleNamespace(name=name, display_name=display_name, tools=[SimpleNamespace(name=tool) for tool in tools])


def test_tool_name_clash_drops_the_later_copy_with_a_notice():
    first = _resource("a", ["search_products", "get_product_profile"], "产品数据")
    second = _resource("b", ["search_products", "get_product_profile", "only_b"], "企业数据")

    kept, notices = unique_mcp_tools({"view_text_file"}, [first, second])

    assert [tool.name for tool in kept] == ["search_products", "get_product_profile", "only_b"]
    assert kept[0] in first.tools and kept[2] in second.tools
    assert notices == [
        "MCP「企业数据」的工具 search_products、get_product_profile "
        "与「产品数据」重名，本轮已忽略「企业数据」中的这些工具。"
    ]


def test_native_tool_keeps_its_name_over_an_mcp_tool():
    kept, notices = unique_mcp_tools({"search_products"}, [_resource("a", ["search_products"], "产品数据")])
    assert kept == []
    assert notices and "与「内置工具」重名" in notices[0]
