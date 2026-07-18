#!/usr/bin/env python3
"""streamable-http MCP server：对话建站发布（把沙箱静态站目录发布为平台托管站点）。

身份 / 会话经 HTTP 头注入（由后端 agent_factory 对所有 MCP 统一设置）：
    X-Current-User-Id     当前用户（站点归属，缺失则拒绝）
    X-Chat-Id             当前会话 id（web 主对话的沙箱会话键，取回站点目录）
    X-Conversation-Id     外部渠道会话 id（钉钉等才有值；作 X-Chat-Id 的兜底）

本工具是「站点」插件的核心能力，替代早期的内置 publish_site 原生工具。发布动作需要
沙箱访问权（在 backend 侧），所以本 server 只做转发：把参数 + 身份 POST 到 backend 的
``/v1/internal/sites/publish`` 完成。见 api/routes/v1/internal_sites.py。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.site_publish_mcp import impl

mcp = FastMCP("hugagent-site-publish")

_HDR_USER = "x-current-user-id"
_HDR_CHAT = "x-chat-id"
_HDR_CONV = "x-conversation-id"


def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        v = ctx.request_context.request.headers.get(name)
        return v or None
    except Exception:
        return None


@mcp.tool()
async def publish_site(
    title: str,
    src_dir: str = "",
    source_dir: str = "",
    slug: str = "",
    site_id: str = "",
    visibility: str = "public",
    description: str = "",
    team_id: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把当前站点项目（或指定沙箱目录）发布为平台托管站点，返回可直接访问的 URL。

    适用场景：用户要求「做一个网站/页面/门户/展示站/H5 并能访问」。
    发布分两条路径，按站点形态选择：

    ① 静态站（手写 HTML/CSS/JS）：
      1) 用 write/bash 在**当前项目文件夹**里生成完整静态站
         （必须有 index.html 入口；可以有子目录、css/js/图片）；
      2) publish_site(title='站点名') → 返回 url（src_dir 留空，后端自动取项目文件夹）；
      3) 把 url 以 markdown 链接交付给用户（形如 /site/<slug>/）。

    ② 构建型站点（React/Vite 工程，沙箱内 npm run build 后发布产物）：
      1) 按 site-builder 技能的 React 流程建工程、构建出静态产物；
      2) publish_site(title='站点名',
                      src_dir='/workspace/.site-dist/<工程名>',    ← 构建产物目录
                      source_dir='<源码工程目录>')                 ← 两个参数都必传
         产物进站点托管，**源码工程**镜像进项目文件夹（保证「编辑」进来的是
         可改的源码而不是编译产物）；漏传 source_dir 会导致项目里的源码被产物覆盖。

    编辑已发布站点：从站点卡片「编辑」进入的会话里，项目文件夹已带回源码文件。
    静态站直接改完调 publish_site(title='站点名')；构建型工程（项目里有 package.json）
    改源码 → 重新构建 → 仍按 ② 带双参数发布。均无需 site_id，后端按项目自动定位。
    非项目会话（罕见）：显式传 src_dir 指定沙箱站点根目录；更新已有站点带 site_id。

    限制：静态文件 + 平台内置轻后端 API；≤300 文件、总量 ≤30MB；
    外部 CDN 资源在内网环境可能不可达，样式/脚本尽量内联或本地化。
    公开站点在浏览器里以沙箱模式运行（无 cookie/localStorage），
    不要生成依赖登录态或本地存储的逻辑；需要持久化用下面的站点 API。

    站点内置轻后端 API（站内 JS 用相对路径 fetch，已配好 CORS）：
      - KV 存储（计数器/游戏分数/配置）：
          读 GET  __api/kv/<key>            → {value, exists}
          写 PUT  __api/kv/<key>  body: {"value": "..."}（≤4KB，≤200 键）
      - 表单收集（留言/报名/反馈，站主可在站点管理里导出 CSV）：
          POST __api/forms/<form_key>  body: 扁平 JSON 对象（≤8KB）
      注意：__api/ 是保留前缀，站点文件不能用这个目录名；
      fetch 相对路径必须不带前导 /（如 fetch('__api/kv/score')）。

    Args:
        title (`str`): 站点标题（显示在用户的站点管理列表）。
        src_dir (`str`, 可选): 沙盒里要发布的站点内容根目录（在 /workspace/ 下）。
            静态站的项目会话留空即可（后端自动定位项目文件夹）；构建型站点必须
            指向构建产物目录（如 /workspace/.site-dist/<工程名>）。
        source_dir (`str`, 可选): 构建型站点的**源码工程目录**。传入后发布产物进
            托管、源码镜像进项目文件夹。静态站不传。
        slug (`str`, 可选): 自定义访问路径（3-50 位小写字母/数字/连字符）。不传则自动生成。
        site_id (`str`, 可选): 更新已有站点时传（首次发布返回值里有）。
        visibility (`str`, 可选): public=任何人凭链接访问（默认）；
            private=仅用户本人登录后可见；team=指定团队成员可见。
        description (`str`, 可选): 一句话站点描述。
        team_id (`str`, 可选): visibility=team 时的授权团队；不传则用用户所在的第一个团队。

    Returns:
        JSON: {ok, site_id, slug, url, title, visibility, version, file_count,
        total_size_bytes, mirrored_from} 或 {error: '...'}。
    """
    return await impl.publish_site(
        user_id=_hdr(ctx, _HDR_USER) or "",
        chat_id=_hdr(ctx, _HDR_CHAT) or _hdr(ctx, _HDR_CONV) or "",
        src_dir=src_dir,
        source_dir=source_dir,
        title=title,
        slug=slug,
        site_id=site_id,
        visibility=visibility,
        description=description,
        team_id=team_id,
    )


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9113)


if __name__ == "__main__":
    main()
