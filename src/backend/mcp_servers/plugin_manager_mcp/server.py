#!/usr/bin/env python3
"""streamable-http MCP server：插件管理（搜索/详情/安装/导入/启停/卸载插件）。

用户身份经 HTTP 头注入（由后端 agent_factory 设置）：
    X-Current-User-Id     当前用户（所有操作按它归属，缺失则拒绝）

工具直连后端 DB / 复用 plugin_service，不跨用户。插件包的创作与下载由本插件打包的
plugin-creator 技能在沙箱内完成，产物经共享产物库交给 import_plugin 落库——
与 skill-manager 的 register_skill 是同一条通路。
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
async def install_plugin(
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


@mcp.tool()
async def list_my_plugins(ctx: Context | None = None) -> Dict[str, Any]:
    """列出我的插件（install_id / slug / 名称 / 是否启用 / 带了几个组件）。

    用户问"我装了哪些插件 / 管一下我的插件"时调用。
    列表里 is_global=true 的是管理员下发的全局插件——**你只能看，不能停用也不能卸载**；
    其余是用户自己装的，可以用 set_plugin_enabled 停用启用、uninstall_plugin 卸载。
    """
    return impl.list_my_plugins(user_id=_user(ctx))


@mcp.tool()
async def import_plugin(
    artifact_id: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把一个插件包导入成我的插件（用于"从网址装"或"自己攒一个"）。

    【配合 plugin-creator 技能使用】标准流程：
      1. 在沙箱 /workspace 里准备好插件目录（含 plugin.json；从网址装就先 curl 下载解压）。
      2. 用 plugin-creator 带的自检脚本过一遍，别把结构不对的包导进去。
      3. 打包：bash `tar -czf /workspace/plugin.tgz -C <插件目录> .`
      4. 调框架自带的 sandbox_get_artifact("/workspace/plugin.tgz") 取得 artifact_id。
      5. 把该 artifact_id 传给本工具落库。
    包里必须有 plugin.json；如果用户其实只想装一个单独的技能，请改用技能管理插件的
    register_skill，而不是硬塞进这里。导入结果始终是仅自己可见的私有插件。
    【铁律】未成功拿到 ✅ 前不要声称已导入。需要"自助导入插件"权限。
    """
    return impl.import_plugin(user_id=_user(ctx), artifact_id=artifact_id)


@mcp.tool()
async def set_plugin_enabled(
    plugin_ref: str,
    enabled: bool,
    component_kind: str = "",
    component_id: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """停用或重新启用一个已装插件，数据都留着，随时可以再打开。

    用户说"先别用那个插件 / 把它关掉 / 重新打开 / 那个工具太吵"时调用。
    - plugin_ref：install_id、slug 或名称（取自 list_my_plugins）。
    - enabled：true=启用，false=停用。
    - component_kind + component_id：只关插件里的**某一个**组件而不是整个插件时用，
      kind 取 skill 或 mcp，两个参数必须同时给。
    【重要】用户只是嫌某个工具碍事的时候，**优先用停用而不是卸载**——卸载会删数据且不可恢复。
    只能操作自己安装的插件；管理员下发的全局插件在这里改不了。
    """
    return impl.set_plugin_enabled(
        user_id=_user(ctx),
        plugin_ref=plugin_ref,
        enabled=enabled,
        component_kind=component_kind,
        component_id=component_id,
    )


@mcp.tool()
async def uninstall_plugin(
    plugin_ref: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """卸载一个插件，并把它带进来的技能和工具一并删除（不可恢复）。

    plugin_ref 传 install_id、slug 或名称（取自 list_my_plugins）。
    【铁律】卸载前**必须先用 list_my_plugins 或 get_plugin_info 列出"会被一起删掉哪些东西"
    给用户确认**，得到明确同意才动手；匹配到多个时必须先问清楚是哪一个，禁止猜删。
    未拿到 ✅ 前不要声称已卸载。
    只能卸载自己安装的插件；管理员下发的全局插件卸不了，本插件（插件管理）自己也卸不了。
    如果用户只是暂时不想用，改用 set_plugin_enabled 停用。
    """
    return impl.uninstall_plugin(user_id=_user(ctx), plugin_ref=plugin_ref)


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9116)


if __name__ == "__main__":
    main()
