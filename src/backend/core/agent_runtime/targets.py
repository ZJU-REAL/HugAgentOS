"""Resolve which agent a turn runs: mode bindings, @mentions, direct sub-agent chats, project scope."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import core.projects.edition as project_scope

logger = logging.getLogger(__name__)

_PROJECT_CTX_KEYS = (
    "project_id",
    "project_api_scope",
    "project_name",
    "project_instructions",
    "project_init",
    "project_folder_name",
    "project_folder_kind",
    "project_folder_id",
    # Desktop local project — needed so the prompt renders the local-mode section
    # with the real host folder path instead of the cloud /workspace section.
    "project_kind",
    "project_is_local",
    "project_local_path",
    "project_local_slug",
) + project_scope.edition_project_context_keys()


def _mode_visible_subagents(spec, visible, mentioned_ids):
    """收窄模式下最终可见的子智能体 = 模式圈定的那批 ∪ 本轮被显式 @ 的那个。

    模式没圈任何子智能体时维持原契约：只有本轮被呼唤的那个能入场（极速模式一直
    是这么做的）。圈了就以模式名单为准，再并上本轮呼唤的，免得用户 @ 了一个模式
    里没列的智能体时那次呼唤被无声吞掉。
    """
    allowed = {
        a for a in (getattr(spec, "agent_ids", ()) or ()) if isinstance(a, str) and a.strip()
    }
    wanted = {str(m) for m in (mentioned_ids or []) if m}
    keep = allowed | wanted
    if not keep:
        return []
    return [item for item in (visible or []) if str((item or {}).get("agent_id") or "") in keep]


def _runtime_visible_subagents(
    user_agents,
    parent_runtime,
    disabled_builtin_ids,
    explicit_agent_ids,
):
    """Build the default agent directory plus any agent summoned this turn.

    Disabled agents stay out of the main agent's initial context. An explicit
    ``@``/command may add exactly the selected accessible agent for this turn;
    the route layer has already enforced ownership and platform visibility.
    """
    from core.capabilities.agents.builtin import merge_builtin_subagents

    explicitly_selected = {str(agent_id) for agent_id in (explicit_agent_ids or []) if agent_id}
    all_accessible = merge_builtin_subagents(
        user_agents,
        parent_runtime,
        disabled_agent_ids=disabled_builtin_ids,
        include_disabled=True,
    )
    return [
        item
        for item in all_accessible
        if item.get("is_enabled", True) or str(item.get("agent_id") or "") in explicitly_selected
    ]


def _resolve_mode_spec(context: dict):
    """从 context 解析这段对话的模式装配契约。

    线上字段是 ``mode_slug``；老客户端只发 ``chat_mode='turbo'``，按极速模式解析，
    所以升级期间新旧前端都能跑。解析失败返回 None（= 标准模式，不收窄）。
    """
    slug = str(context.get("mode_slug", "") or "").strip()
    if not slug and str(context.get("chat_mode", "") or "").lower() == "turbo":
        slug = "turbo"
    if not slug:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.chat.modes import ChatModeService

        with SessionLocal() as db:
            spec = ChatModeService(db).resolve(slug, str(context.get("user_id", "") or "") or None)
        return spec
    except Exception:  # noqa: BLE001 — 模式是增强不是边界，解析炸了也要让对话跑起来
        logger.warning("[workflow] 模式解析失败 slug=%s，按标准模式继续", slug, exc_info=True)
        return None


def _approval_mode(context: Dict[str, Any]) -> Optional[str]:
    """本轮上下文里带的权限档；没带就交回 None，由工厂读用户自己存的那一档。"""
    return context.get("approval_mode")


def _extract_project_ctx(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract project-related fields from the workflow context. Returns None when there is no project_id."""
    if not context.get("project_id"):
        return None
    return {k: context.get(k) for k in _PROJECT_CTX_KEYS}


def _parse_agent_mentions(message: str, available_agents: list) -> list:
    """Parse @agent_name mentions from user message.

    Returns list of matched ``agent_id``s in the order they appear in
    *message*.

    When several agent names share a prefix (e.g. ``搜索`` vs ``搜索助手``)
    a naive ``"@搜索" in message`` check matches "@搜索" *inside*
    "@搜索助手", so typing ``@搜索助手`` would falsely also mention the
    shorter ``搜索`` agent — and the prompt hint built downstream would
    instruct the LLM to call both. We sort candidates by name length
    descending and reserve consumed character ranges so the longest
    matching name wins and prefix-shadow matches are skipped.
    """
    if not available_agents:
        return []

    candidates = [a for a in available_agents if a.get("name")]
    if not candidates:
        return []
    candidates.sort(key=lambda a: len(a["name"]), reverse=True)

    consumed: list = []  # non-overlapping (start, end) char ranges

    def _overlaps(start: int, end: int) -> bool:
        for s, e in consumed:
            if start < e and end > s:
                return True
        return False

    # (position, agent_id) — sorted at the end to preserve message order
    hits: list = []
    for agent in candidates:
        token = "@" + agent["name"]
        start = 0
        while True:
            idx = message.find(token, start)
            if idx < 0:
                break
            end = idx + len(token)
            if not _overlaps(idx, end):
                consumed.append((idx, end))
                hits.append((idx, agent["agent_id"]))
            start = idx + len(token)

    hits.sort(key=lambda x: x[0])
    seen: set = set()
    ordered: list = []
    for _, aid in hits:
        if aid not in seen:
            seen.add(aid)
            ordered.append(aid)
    return ordered


def _direct_agent_id_from_context(context: Dict[str, Any]) -> Optional[str]:
    """Return the target only for a persistent dedicated sub-agent chat.

    A per-turn @mention must stay on the main-model stream so it produces a
    genuine ``call_subagent`` tool call instead of silently bypassing the
    parent model's reasoning and streaming lifecycle.
    """
    value = context.get("agent_id")
    return str(value) if value else None


def _load_direct_user_agent(
    agent_id: str,
    user_id: str,
    parent_runtime: Optional[Dict[str, Any]] = None,
):
    """Load a built-in profile or detach a complete custom-agent configuration."""
    from core.db.engine import SessionLocal
    from core.capabilities.agents.builtin import build_builtin_runtime_profile, get_builtin_subagent
    from core.capabilities.agents.edition import UserAgentService

    builtin_spec = get_builtin_subagent(agent_id)
    if builtin_spec is not None:
        return build_builtin_runtime_profile(builtin_spec, parent_runtime)

    with SessionLocal() as db:
        user_agent = UserAgentService(db).get_raw_by_id(agent_id, user_id=user_id)
        # Eagerly load every relationship/config field consumed after the DB
        # session closes. SQLAlchemy expires lazy relationships on close.
        _ = user_agent.mcp_server_ids, user_agent.skill_ids, user_agent.kb_ids
        _ = user_agent.system_prompt, user_agent.model_provider_id
        _ = user_agent.max_iters, user_agent.temperature, user_agent.max_tokens
        _ = user_agent.timeout, user_agent.extra_config
        return user_agent
