"""Subagent directory and per-turn routing prompts."""

from typing import Any, Dict, List, Optional


def _collapse_ids(ids: List[str], *, min_group: int = 3) -> str:
    """Render an id list, folding same-prefix families into ``prefix-*（N 个）``.

    A bundle installs skills under auto-generated slugs
    (``industry-knowledge-center-enterprise-prof-ea63ac18``); sixteen of them
    inline is ~700 characters of the system prompt on every request, and the
    hash suffixes carry no routing signal — the router picks a sub-agent by
    what family of work it can do, then delegates by agent_id. Folding keeps
    that signal at a fraction of the prefill cost. Families smaller than
    ``min_group`` are listed in full, so short lists render exactly as before.
    """
    groups: Dict[str, List[str]] = {}
    for raw in ids:
        sid = str(raw)
        # Family = first three dash-separated tokens (bundle slugs are
        # ``<bundle-name>-<topic>-<hash>``); shorter ids group under themselves.
        parts = sid.split("-")
        key = "-".join(parts[:3]) if len(parts) > 3 else sid
        groups.setdefault(key, []).append(sid)

    out: List[str] = []
    for key, members in groups.items():
        if len(members) >= min_group:
            out.append(f"{key}-*（{len(members)} 个）")
        else:
            out.extend(members)
    return ", ".join(out)


def _get_tools_desc(agent_info: Dict[str, Any]) -> str:
    """Return the effective, role-filtered capability summary shown to the router."""
    mcp_ids = agent_info.get("mcp_server_ids") or []
    skill_ids = agent_info.get("skill_ids") or []
    kb_ids = agent_info.get("kb_ids") or []
    extra = agent_info.get("extra_config") or {}
    builtin_role = extra.get("builtin_role")

    if not builtin_role:
        return ", ".join(mcp_ids) if mcp_ids else "按该子智能体自身配置"

    capabilities: List[str] = ["制品读取"]
    sandbox_enabled = bool(extra.get("sandbox_tools_enabled"))
    code_enabled = bool(extra.get("code_capability_enabled"))

    if sandbox_enabled:
        capabilities.append("沙盒制品读写")
        capabilities.append("Bash")
    if code_enabled:
        capabilities.append("Read/Edit/Write/Glob/Grep/Bash")
    if skill_ids:
        capabilities.append("技能：" + _collapse_ids(skill_ids))
    if mcp_ids:
        capabilities.append("MCP：" + _collapse_ids(mcp_ids))
    if kb_ids:
        capabilities.append("知识库：" + _collapse_ids(kb_ids))
    return "；".join(capabilities)


def build_subagent_prompt_section(
    visible_agents: List[Dict[str, Any]],
) -> str:
    """Build the system prompt section describing available sub-agents.

    Per-turn @-mention hints are returned by build_subagent_mention_hint() and
    are kept OUT of the system prompt. Otherwise the system prompt would vary
    every turn (whenever the user @s different agents), defeating the LLM
    provider's prefix cache.
    """
    if not visible_agents:
        return ""

    rows = []
    shared_agents = []
    for a in visible_agents:
        desc = a.get("description", "")
        tools = _get_tools_desc(a)
        has_shared = (a.get("extra_config") or {}).get("shared_context", False)
        ctx_col = "继承主对话" if has_shared else "独立简报"
        rows.append(f"| {a['agent_id']} | {a['name']} | {desc} | {tools} | {ctx_col} |")
        if has_shared:
            shared_agents.append(a["name"])

    table = (
        "| ID | 名称 | 适用场景 | 本轮可用能力 | 上下文策略 |\n"
        "|---|---|---|---|---|\n" + "\n".join(rows)
    )

    section = (
        "## 可用子智能体\n"
        "你可以用 `call_subagent` 把一段完整的工作交给下表中的子智能体。按「适用场景」选择角色；"
        "「本轮可用能力」就是它这一轮的全部能力，子智能体不能再往下委派。\n\n"
        + table + "\n\n"
        "### 何时分派\n"
        "任务较为复杂、任务需要调用子智能体才有的工具、用户用 @名称 指定了它，或需要并行查多个独立信息源时，再分派。"
        "一步能做完的事、简单问答、读文件跑命令这类琐事，以及下一步直接依赖上一步结果的任务都自己做无需委派"
        "难以交代或与主线耦合太紧的任务，都自己做，不要派出去再干等。"
        "判断问题出在哪、该怎么改，必须由你自己完成。\n\n"
        "### 编写 task\n"
        "标注「独立简报」的子智能体看不到对话历史，task 要写清目标和用途、已知和已排除的信息、"
        "完成标准，以及具体要查什么、回答什么；标注「继承主对话」的只需写要做的操作。"
        "子智能体在同一工作区中运行，并行的写任务要划分互不重叠的文件范围，拆不开就串行。\n\n"
        "### 续跑与结果\n"
        "把返回的句柄填进 `resume_session_id`，可以在原会话上纠错或补做；"
        "方向错了、换角度重查或审查他人产出时，新开一个。重派时说明上次具体哪里失败。"
        "用户批准的危险动作，把原话逐字写进 task 并新开执行。"
        "子智能体的回复用户看不到，你要整合后再呈现；涉及改动时，先自己核验实际产出再汇报。\n"
    )

    return section


def build_subagent_mention_hint(
    visible_agents: List[Dict[str, Any]],
    mentioned_agent_ids: Optional[List[str]] = None,
) -> str:
    """Build a per-turn hint when the user @-mentioned specific subagents.

    Designed to be prepended to the *current user message* so the system prompt
    stays byte-stable across turns (prefix-cache friendly). Returns "" when
    there's nothing to inject.
    """
    if not mentioned_agent_ids or not visible_agents:
        return ""
    agent_map = {a["agent_id"]: a["name"] for a in visible_agents}
    names = [agent_map.get(aid, aid) for aid in mentioned_agent_ids if aid in agent_map]
    if not names:
        return ""
    return (
        f"**用户已指定调用子智能体：{'、'.join(names)}。"
        "请直接使用 call_subagent 工具调用指定的子智能体。**\n"
    )


def build_explicit_subagent_command_hint(
    visible_agents: List[Dict[str, Any]],
    agent_id: str,
) -> str:
    """Guide explicit delegation while preserving the normal model loop."""
    target = next(
        (item for item in visible_agents if str(item.get("agent_id") or "") == agent_id),
        None,
    )
    if not target:
        return ""
    agent_name = str(target.get("name") or agent_id)
    return (
        "<explicit_subagent_command>\n"
        f"用户已明确要求调用子智能体「{agent_name}」（agent_id={agent_id}）。\n"
        "请优先按任务需要使用 "
        f'call_subagent(agent_id="{agent_id}", task=<下方用户任务>)。\n'
        "可按任务需要使用其他工具辅助。"
        "子智能体返回后，结合其结果继续完成用户任务。\n"
        "</explicit_subagent_command>"
    )
