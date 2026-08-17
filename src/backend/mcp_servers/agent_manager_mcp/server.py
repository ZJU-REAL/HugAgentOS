#!/usr/bin/env python3
"""streamable-http MCP server：智能体管理（搜索/安装/创建/修改/删除/申请上架子智能体）。

用户身份经 HTTP 头注入（由后端 agent_factory 设置）：
    X-Current-User-Id     当前用户（所有操作按它归属，缺失则拒绝）

工具直连后端 DB / 复用 UserAgentService·agent_market_service，不跨用户。
与 skill-manager 的差别：子智能体没有文件产物，所有字段都能直接作为工具入参传，
因此这里不需要沙箱与共享产物库通路；本插件打包的 agent-designer 技能只给"怎么设计"的方法。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.agent_manager_mcp import impl

mcp = FastMCP("hugagent-agent-manager")

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
async def search_agent_market(
    query: str = "",
    category: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """搜索子智能体市场，返回可安装的智能体列表（slug/名称/简介/分类/是否已安装）。

    用户想"有没有现成的 X 智能体 / 市场里都有什么 / 找一个能做 Y 的智能体"时调用。
    - query：关键词（在 slug/名称/简介/标签/分类里模糊匹配；留空=列全部）。
    - category：按分类过滤（可选）。
    找到目标后用 install_market_agent(slug) 安装。
    """
    return impl.search_agent_market(user_id=_user(ctx), query=query, category=category)


@mcp.tool()
async def install_market_agent(
    slug: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """从市场安装一个子智能体到"我的智能体"，装完下一轮对话就能直接指派任务给它。

    用户说"装上那个 / 安装 X 智能体 / 我要用它"时调用。slug 取自 search_agent_market。
    安装是**克隆一份到你名下**，之后随便改都不影响市场原件；它原本绑定的技能和工具会按
    你账下实际有的资源重新对应，对应结果在 install_report 里，**有对不上被跳过的必须如实
    告诉用户**，别让用户以为能力全绑上了。
    【铁律】未成功拿到 ✅ 前不要声称已安装。需要管理员开启"自助添加智能体"权限。
    """
    return impl.install_market_agent(user_id=_user(ctx), slug=slug)


@mcp.tool()
async def list_bindable_capabilities(ctx: Context | None = None) -> Dict[str, Any]:
    """列出可以绑给子智能体的技能、工具、插件、知识库，每项都带 id 和名称。

    【创建或修改子智能体之前必须先调用它拿到准确的 id】——绝不允许凭印象编 id，
    编错了不会报错，只会静默绑不上，用户拿到一个"看起来建好了但没能力"的智能体。
    返回四组：skills（技能）/ mcp_servers（工具）/ plugins（插件，整体绑）/ kb_spaces（知识库）。
    """
    return impl.list_bindable_capabilities(user_id=_user(ctx))


@mcp.tool()
async def create_agent(
    name: str,
    description: str,
    system_prompt: str,
    skill_ids: List[str] | None = None,
    mcp_server_ids: List[str] | None = None,
    plugin_ids: List[str] | None = None,
    kb_ids: List[str] | None = None,
    max_iters: int | None = None,
    welcome_message: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """从零创建一个属于我的子智能体。

    用户说"帮我建一个负责 X 的智能体 / 我想要个专门做 Y 的助手"时调用。
    - name：名字。
    - description：一句话说清它管什么（也是主智能体判断"该不该派活给它"的依据，要具体）。
    - system_prompt：它的行事准则，**是这个智能体的主体**——角色、能做什么、不能做什么、
      输出成什么样。怎么写见本插件打包的 agent-designer 技能，别糊一段空泛的话交差。
    - skill_ids / mcp_server_ids / plugin_ids / kb_ids：绑给它的能力，
      **id 必须来自 list_bindable_capabilities**。宁窄勿宽，只绑真正用得着的。
    - max_iters：一次任务最多干几轮（1–100，默认 10）。
    建出来的智能体仅自己可见可用，下一轮对话即可指派任务。
    【铁律】未成功拿到 ✅ 前不要声称已创建。需要管理员开启"自助添加智能体"权限。
    """
    return impl.create_agent(
        user_id=_user(ctx),
        name=name,
        description=description,
        system_prompt=system_prompt,
        skill_ids=skill_ids,
        mcp_server_ids=mcp_server_ids,
        plugin_ids=plugin_ids,
        kb_ids=kb_ids,
        max_iters=max_iters,
        welcome_message=welcome_message,
    )


@mcp.tool()
async def list_my_agents(ctx: Context | None = None) -> Dict[str, Any]:
    """列出"我的子智能体"（agent_id / 名字 / 职责 / 是否启用 / 绑了几项能力）。

    用户问"我有哪些智能体 / 我建过什么 / 管一下我的智能体"时调用。
    拿到 agent_id 后可用 edit_agent 修改、delete_agent 删除、submit_agent_to_market 申请上架。
    只列你自己建的和装的；平台自带的探索员/执行员/审查员以及管理员下发的智能体不在此列。
    """
    return impl.list_my_agents(user_id=_user(ctx))


@mcp.tool()
async def edit_agent(
    agent_ref: str,
    name: str | None = None,
    description: str | None = None,
    system_prompt: str | None = None,
    skill_ids: List[str] | None = None,
    mcp_server_ids: List[str] | None = None,
    plugin_ids: List[str] | None = None,
    kb_ids: List[str] | None = None,
    max_iters: int | None = None,
    is_enabled: bool | None = None,
    welcome_message: str | None = None,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """修改我的某个子智能体，不用删了重建。agent_ref 传 agent_id 或名字。

    用户说"把 X 的职责改一下 / 给它加个技能 / 换个说话的口气 / 让它别再用那个工具"时调用。
    **只改你传的字段，没传的原样保留**（字段级部分更新）。
    - name / description / system_prompt / welcome_message：文本字段。
    - skill_ids / mcp_server_ids / plugin_ids / kb_ids：**整组替换，不是追加**。
      所以要"加一个"时：先 list_my_agents 看现在绑了什么 → 再 list_bindable_capabilities
      取新 id → 把旧的和新的合并成完整数组一起传，否则会把原有绑定冲掉。
    - max_iters（1–100）/ is_enabled（临时停用或启用）。
    agent_id 不可改；要换 id 只能删了重建。名字匹配到多个时会返回候选，
    必须先让用户指明具体哪一个，禁止猜着改。只能改本人的子智能体。
    【铁律】未成功拿到 ✅ 前不要声称已修改。需要"自助添加智能体"权限。
    """
    return impl.edit_agent(
        user_id=_user(ctx),
        agent_ref=agent_ref,
        name=name,
        description=description,
        system_prompt=system_prompt,
        skill_ids=skill_ids,
        mcp_server_ids=mcp_server_ids,
        plugin_ids=plugin_ids,
        kb_ids=kb_ids,
        max_iters=max_iters,
        is_enabled=is_enabled,
        welcome_message=welcome_message,
    )


@mcp.tool()
async def delete_agent(
    agent_ref: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """删除我的某个子智能体（不可恢复）。agent_ref 传 agent_id 或名字。

    用户说"把 X 删了 / 不要那个智能体了 / 移除它"时调用。
    【铁律】匹配到多个时必须先向用户确认具体哪一个，禁止猜删；未拿到 ✅ 前不要声称已删除。
    只能删自己创建/安装的，平台内置角色和管理员下发的智能体删不了。
    如果用户只是暂时不想用，优先建议 edit_agent(is_enabled=false) 停用而不是删除。
    """
    return impl.delete_agent(user_id=_user(ctx), agent_ref=agent_ref)


@mcp.tool()
async def submit_agent_to_market(
    agent_id: str,
    category: str = "",
    summary: str = "",
    note: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """把我的子智能体申请上架到市场（进管理员审核队列，通过后其他人可安装）。

    用户说"把这个分享出去 / 申请上架 / 发布到市场"时调用。agent_id 取自 list_my_agents。
    - category：市场分类，**必须**从这 9 个固定值里挑最贴切的一个（注意与技能市场的分类不同）：
      通用助手 / 职场办公 / 商业分析 / 数据分析 / 研发编程 / 翻译写作 / 创意设计 / 政策法务 / 教育科研。
    - summary：一句话简介（可选）。
    - note：给审核管理员的说明（可选）。
    【说明】这是"申请"，不是直接上架；要跟用户说清楚还得等管理员审核通过。
    需要"自助添加智能体"权限。
    """
    return impl.submit_agent_to_market(
        user_id=_user(ctx),
        agent_id=agent_id,
        category=category,
        summary=summary,
        note=note,
    )


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9115)


if __name__ == "__main__":
    main()
