#!/usr/bin/env python3
"""Cloud MCP endpoint for the skill-manager plugin.

The versioned manager contract declares cloud artifact installation. Desktop
plugin execution projects that declaration to device paths through its local
adapter. Uploading to a private cloud account and marketplace submission are
separate actions. Caller ownership comes from X-Current-User-Id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.skill_manager_mcp import impl

mcp = FastMCP("hugagent-skill-manager")

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
async def search_marketplace(
    query: str = "",
    category: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """搜索技能市场，返回匹配的可安装技能列表（slug/名称/简介/分类/是否已安装）。

    用户想"看看有没有现成的 X 技能 / 技能市场里有什么 / 找一个能做 Y 的技能"时调用。
    - query：关键词（在名称/简介/标签/分类里模糊匹配；留空=列全部）。
    - category：按分类过滤（可选）。
    找到目标后用 install_from_marketplace(slug) 安装。
    """
    return impl.search_marketplace(user_id=_user(ctx), query=query, category=category)

@mcp.tool()
async def install_from_marketplace(
    slug: str,
    secrets: Dict[str, str] | None = None,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """从技能市场安装一个技能到"我的私有技能库"，装完即可在对话中使用。

    用户说"装上那个技能 / 安装 X 技能 / 把它加到我的能力里"时调用。slug 取自 search_marketplace。
    若该技能需要 API Key 等凭据（返回报错提示缺凭据），先向用户要，再重试。
    - secrets：按市场技能 required_secrets 的 key 传入凭据，例如 {"IMAGE_GEN_API_KEY":"..."}。
    【铁律】未成功拿到 ✅ 前不要声称已安装。需要管理员开启"自助添加技能"权限。
    """
    return impl.install_from_marketplace(user_id=_user(ctx), slug=slug, secrets=secrets or {})

@mcp.tool()
async def submit_to_marketplace(
    skill_id: str,
    category: str = "",
    summary: str = "",
    note: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把我的私有技能申请上架到技能市场（进管理员审核队列，通过后其他人可安装）。

    用户说"把我的技能分享出去 / 申请上架 / 发布到市场"时调用。skill_id 取自云端 list_skills 的 install_id。
    - category：市场分类，**必须**从这 8 个固定值里挑最贴切的一个：
      写作助手 / 文档处理 / 数据分析 / 政策产业 / 营销创意 / 法务合规 / 办公效率 / 研发效率。
    - summary：一句话简介（可选）。
    - note：给审核管理员的说明（可选）。
    【说明】这是"申请"，不是直接上架；需管理员审核通过。需要"自助添加技能"权限。
    """
    return impl.submit_to_marketplace(
        user_id=_user(ctx), skill_id=skill_id, category=category, summary=summary, note=note
    )

from mcp_servers.management_registration import register
register(mcp, "skill-manager", _user)

def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9112)

if __name__ == "__main__":
    main()
