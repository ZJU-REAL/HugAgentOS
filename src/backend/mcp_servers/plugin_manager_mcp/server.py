#!/usr/bin/env python3
"""Cloud MCP endpoint for the plugin-manager plugin.

The versioned manager contract declares cloud artifact installation. Desktop
plugin execution projects that declaration to device paths through its local
adapter. Uploading to a private cloud account and marketplace submission are
separate actions. Caller ownership comes from X-Current-User-Id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.plugin_manager_mcp import impl

mcp = FastMCP("hugagent-plugin-manager")

_HDR_USER = "x-current-user-id"

def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        v = ctx.request_context.request.headers.get(name)
        return v or None
    except Exception:
        return None

def _user(ctx: Optional[Context]) -> str:
    return _hdr(ctx, _HDR_USER) or ""

@mcp.tool()
async def search_plugin_market(
    query: str = "",
    category: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """搜索插件市场，返回可安装的插件列表（slug/名称/分类/简介/是否已安装）。

    用户想"有没有能连飞书的插件 / 插件市场里有什么 / 找个能做 X 的插件"时调用。
    - query：关键词（在 slug/名称/简介/分类里模糊匹配；留空=列全部）。
    - category：按分类过滤（可选）。
    找到目标后**建议先用 get_plugin_info(slug)** 看看它会带进来什么，再决定装不装。
    """
    return impl.search_plugin_market(user_id=_user(ctx), query=query, category=category)

@mcp.tool()
async def get_plugin_info(
    slug: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """查看某个插件的详情：会给你装进来哪些技能、哪些工具，需不需要填 API Key 之类的凭据。

    【安装前先调它，把"装了会多出什么"讲给用户听】，别让用户装完才发现还要填密钥。
    用户问"这插件是干嘛的 / 装了会怎样 / 它安全吗"时也用它。
    返回里的 required_secrets 就是安装时要准备的凭据清单。
    """
    return impl.get_plugin_info(user_id=_user(ctx), slug=slug)

@mcp.tool()
async def install_plugin_from_marketplace(
    slug: str,
    secrets: Dict[str, str] | None = None,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """从插件市场安装一个插件到我的空间，装完里面的技能和工具立即可用。

    用户说"装上它 / 安装 X 插件 / 把这个能力加上"时调用。slug 取自 search_plugin_market。
    - secrets：按插件 required_secrets 的 key 传入凭据，例如 {"FIRECRAWL_API_KEY":"..."}。
      如果该插件需要凭据而你没传，会返回缺哪些 key——这时**先向用户要**，拿到后重试，
      不要自己编一个假的填进去。
    【铁律】未成功拿到 ✅ 前不要声称已安装。需要管理员开启"自助导入插件"权限。
    """
    return impl.install_plugin(user_id=_user(ctx), slug=slug, secrets=secrets or {})

from mcp_servers.management_registration import register
register(mcp, "plugin-manager", _user)

def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9116)

if __name__ == "__main__":
    main()
