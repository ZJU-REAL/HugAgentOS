#!/usr/bin/env python3
"""streamable-http MCP server：技能管理（搜索/安装/创建落库/申请上架/管理/删除）。

用户身份经 HTTP 头注入（由后端 agent_factory 设置）：
    X-Current-User-Id     当前用户（所有操作按它归属，缺失则拒绝）

工具直连后端 DB / 复用 marketplace_service·plugin_service·artifacts.store，不跨用户。
"沙箱够不着的"动词才放这里；技能的创作/下载解包由本插件打包的 skill-creator 技能在沙箱内做，
产物经共享产物库交给 register_skill 落库。见 internal design docs。
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
async def register_skill(
    artifact_id: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把在沙箱里创作好的技能"存进我的技能库"（创建私有技能）。

    【配合 skill-creator 技能使用】标准流程：
      1. 先按 skill-creator 技能在沙箱 /workspace 里产出一个技能目录（含 SKILL.md）。
      2. 在沙箱里打包：bash `tar -czf /workspace/skill.tgz -C <技能目录> .`
      3. 调框架自带的 sandbox_get_artifact("/workspace/skill.tgz") 取得 artifact_id。
      4. 把该 artifact_id 传给本工具落库。
    也可用于"从 web 链接安装"：在沙箱里 curl 下载技能/插件包并解压自检后打成 tar，同样走本工具
    （包内若含 plugin.json 会自动按插件导入）。
    自助入口始终落成仅自己可见可用的私有技能。
    【铁律】未成功拿到 ✅ 前不要声称已创建。需要管理员开启"自助添加技能"权限。
    """
    return impl.register_skill(user_id=_user(ctx), artifact_id=artifact_id, make_private=True)


@mcp.tool()
async def list_my_skills(ctx: Context | None = None) -> Dict[str, Any]:
    """列出"我的私有技能"（skill_id / 名称 / 版本 / 启用状态）。

    用户问"我有哪些自己的技能 / 我创建过什么技能 / 管理我的技能"时调用。
    拿到 skill_id 后可用 submit_to_marketplace 申请上架或 delete_skill 删除。
    """
    return impl.list_my_skills(user_id=_user(ctx))


@mcp.tool()
async def submit_to_marketplace(
    skill_id: str,
    category: str = "",
    summary: str = "",
    note: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把我的私有技能申请上架到技能市场（进管理员审核队列，通过后其他人可安装）。

    用户说"把我的技能分享出去 / 申请上架 / 发布到市场"时调用。skill_id 取自 list_my_skills。
    - category：市场分类，**必须**从这 8 个固定值里挑最贴切的一个：
      写作助手 / 文档处理 / 数据分析 / 政策产业 / 营销创意 / 法务合规 / 办公效率 / 研发效率。
    - summary：一句话简介（可选）。
    - note：给审核管理员的说明（可选）。
    【说明】这是"申请"，不是直接上架；需管理员审核通过。需要"自助添加技能"权限。
    """
    return impl.submit_to_marketplace(
        user_id=_user(ctx), skill_id=skill_id, category=category, summary=summary, note=note
    )


@mcp.tool()
async def delete_skill(
    skill_ref: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """删除"我的一个私有技能"（不可恢复）。skill_ref 传 skill_id 或技能名称。

    用户说"删掉我的 X 技能 / 移除那个技能"时调用。
    【铁律】匹配到多个技能时必须先向用户确认具体哪一个，禁止猜删；未拿到 ✅ 前不要声称已删除。
    只能删自己创建/安装的私有技能，删不了公共技能。
    """
    return impl.delete_skill(user_id=_user(ctx), skill_ref=skill_ref)


@mcp.tool()
async def edit_skill(
    skill_ref: str,
    display_name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tags: list[str] | None = None,
    version: str | None = None,
    files_upsert: Dict[str, str] | None = None,
    files_delete: list[str] | None = None,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """修改"我的一个已有私有技能"的内容（无需删了重建）。skill_ref 传 skill_id 或技能名称。

    用户说"改一下我那个 X 技能 / 把技能的描述/正文/名字改成…… / 给技能加个文件 / 更新技能里的脚本"
    时调用。**只更新你传入的字段，没传的保持原样**（字段级部分更新）：
    - display_name：技能显示名。
    - description：一句话描述（技能加载硬性必填，不能改成空）。
    - instructions：技能正文（SKILL.md 指令正文，Markdown）——这就是"技能内容/怎么做事"的主体。
    - tags：标签数组（整组替换）。
    - version：版本号。
    - files_upsert：{文件名: UTF-8文本内容} 新增或覆盖技能内的附属文件（如脚本/模板）；SKILL.md 不走这里，改正文用 instructions。二进制大文件请改用 register_skill 打包上传。
    - files_delete：要删除的附属文件名数组。
    技能 id 不可改；要换 id 只能删了重建。匹配到多个技能时会返回候选，必须先让用户指明具体哪一个，禁止猜改。
    只能改本人的私有技能，改不了公共技能。需要管理员开启"自助添加技能"权限。
    【铁律】未成功拿到 ✅ 前不要声称已修改。
    """
    return impl.edit_skill(
        user_id=_user(ctx),
        skill_ref=skill_ref,
        display_name=display_name,
        description=description,
        instructions=instructions,
        tags=tags,
        version=version,
        files_upsert=files_upsert,
        files_delete=files_delete,
    )


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9112)


if __name__ == "__main__":
    main()
