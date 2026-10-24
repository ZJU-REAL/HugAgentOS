"""Workflow agent() forks the shared main-agent factory and runtime."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from typing import Any

_INFRA_FAILURE_MARKERS = (
    "用量限制",
    "quota",
    "rate limit",
    "too many requests",
    "调用失败",
    "connection error",
    "timeout",
    "timed out",
    "503",
    "502",
    "429",
)


def _looks_like_infra_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _INFRA_FAILURE_MARKERS)


def snapshot_parent_runtime(request, *, skill_ids, mcp_ids, sandbox_session_id, progressive=None):
    """Persist configuration references, never model credentials or chat history."""
    skills, mcps = list(skill_ids or []), list(mcp_ids or [])
    for plugin in getattr(progressive, "directory", ()):
        skills.extend(plugin.skill_ids)
        mcps.extend([*plugin.mcp_ids, *plugin.bound_mcp_ids])
    return deepcopy(
        {
            "enabled_skill_ids": list(dict.fromkeys(skills)),
            "enabled_mcp_ids": list(dict.fromkeys(mcps)),
            "enabled_kb_ids": list(request.enabled_kb_ids or []),
            "subagent_ids": [a["agent_id"] for a in request.visible_subagents or []],
            "sandbox_session_id": sandbox_session_id,
            "workspace_id": request.workspace_id,
            "capability_source_chat_id": request.chat_id,
            "project_ctx": request.project_ctx,
            "model_name": request.model_name,
            "model_provider_id": request.model_provider_id,
            "chat_mode": request.chat_mode,
            "turbo_mode": request.turbo_mode,
            "reranker_enabled": request.reranker_enabled,
            "mode_spec": asdict(request.mode_spec) if request.mode_spec is not None else None,
            "read_only": request.read_only,
            "allow_bash": request.allow_bash,
        }
    )


def resolve_subagents(runtime: dict, user_id: str) -> list[dict]:
    """Recheck current accessibility; never add agents absent from the parent."""
    from core.db.engine import SessionLocal
    from core.capabilities.agents.builtin import merge_builtin_subagents
    from core.capabilities.agents.edition import UserAgentService
    from core.auth.users import UserService

    ids = set(runtime.get("subagent_ids") or [])
    if not ids:
        return []
    with SessionLocal() as db:
        agents = UserAgentService(db).list_for_user(user_id)
        disabled = UserService(db).get_disabled_builtin_subagent_ids(user_id)
    accessible = merge_builtin_subagents(agents, runtime, disabled_agent_ids=disabled)
    return [a for a in accessible if a["agent_id"] in ids and a.get("is_enabled", True)]


async def run_derived_agent(
    prompt: str,
    *,
    parent_runtime: dict | None,
    user_id: str,
    chat_id: str,
    run_id: str,
    journal_owner: str,
    model_name: str | None,
    model_provider_id: str | None,
    project_ctx: dict | None = None,
    tools: list[str] | None = None,
):
    """Use the main factory and streaming loop with fresh task-local context."""
    from core.llm.runtime import workspace
    from core.llm.factory import create_agent_executor
    from core.llm.mcp.manager import close_clients
    from core.llm.runtime.streaming import StreamingAgent
    from core.chat.mode_types import ChatModeSpec

    if not isinstance(parent_runtime, dict):
        raise ValueError("作业缺少主智能体运行配置，请重新提交工作流")
    runtime = deepcopy(parent_runtime)
    mode = runtime.get("mode_spec")
    options: dict[str, Any] = {
        "enabled_skill_ids": list(runtime.get("enabled_skill_ids") or []),
        "enabled_mcp_ids": list(runtime.get("enabled_mcp_ids") or []),
        "enabled_kb_ids": list(runtime.get("enabled_kb_ids") or []),
        "visible_subagents": resolve_subagents(runtime, user_id),
        "sandbox_session_id": runtime.get("sandbox_session_id"),
        "workspace_id": runtime.get("workspace_id") or "default",
        "project_ctx": runtime.get("project_ctx") or project_ctx,
        "chat_mode": runtime.get("chat_mode"),
        "reranker_enabled": bool(runtime.get("reranker_enabled")),
        "mode_spec": ChatModeSpec(**mode) if mode else None,
        "approval_mode": "full",
        "isolated": True,
        "derived_main": True,
        "capability_source_chat_id": runtime.get("capability_source_chat_id"),
        "top_level_chat": False,
        "current_user_id": user_id,
        "chat_id": chat_id,
        "run_id": run_id,
        "journal_owner": journal_owner,
        "model_name": model_name,
        "model_provider_id": model_provider_id,
    }
    if runtime.get("turbo_mode"):
        options["turbo_mode"] = True
        options["turbo_explicit_skill_ids"] = options["enabled_skill_ids"]
        options["turbo_explicit_mcp_ids"] = options["enabled_mcp_ids"]
    if runtime.get("read_only"):
        options["read_only"] = True
    if runtime.get("allow_bash") is False:
        options["allow_bash"] = False
    # Existing tools= hints choose initial MCPs; skills and subagents remain available.
    if tools is not None:
        options["enabled_mcp_ids"] = [m for m in tools if m in options["enabled_mcp_ids"]]
    workspace.init_state()
    agent, clients = await create_agent_executor(**options)
    text = ""
    tool_calls = tool_failures = 0
    infra_failure = False
    try:
        async for event, payload in StreamingAgent(agent, clients).stream(
            [{"role": "user", "content": prompt}],
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "run_id": run_id,
                "journal_owner": journal_owner,
                "chat_mode": runtime.get("chat_mode"),
            },
        ):
            if event == "text_delta":
                text += payload
            elif event == "tool_result":
                tool_calls += 1
                content = str((payload or {}).get("content") or "")
                failed = (payload or {}).get("status") in ("error", "denied", "interrupted")
                if failed or _looks_like_infra_failure(content):
                    tool_failures += 1
                    infra_failure |= _looks_like_infra_failure(content)
            elif event == "error":
                infra_failure = True
                break
    finally:
        await close_clients(clients)
    return text, bool(
        infra_failure
        or (tool_calls and tool_failures == tool_calls)
        or _looks_like_infra_failure(text)
    )


async def run_with_job_fence(awaitable, validate, *, interval=1.0):
    """Cancel the owning agent task when its callback authority or budget expires."""
    try:
        await asyncio.to_thread(validate)
    except BaseException:
        awaitable.close()
        raise
    task = asyncio.create_task(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return await task
            await asyncio.to_thread(validate)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
