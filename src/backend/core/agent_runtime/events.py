"""Helpers for the agent event stream: synthesized tool calls, scoped log context, live-job checks."""

from __future__ import annotations
from core.harness import log_context

import logging
from typing import Any, Dict, Optional

import core.config.display_names as display_names

logger = logging.getLogger(__name__)


def _synthesize_missing_tool_call(
    tool_id: str, tool_name: str, displayed_tools: set
) -> Optional[Dict[str, Any]]:
    """Build the ``tool_call`` event a tool never got, or ``None`` if it already has one.

    ``_tool_args_ready`` holds back the tool card while streamed args are still
    incomplete, but it cannot tell "not written yet" from "there is nothing to
    write": a zero-argument tool (for example, a parameterless status probe) keeps
    empty args forever, so its card is suppressed for the whole run. Without a
    card the frontend has nothing to attach the result to and — with tools
    running in parallel — binds it onto whichever sibling is still running,
    making the call vanish from the tool list and briefly corrupting the
    sibling's output. Emitting the missing call right before its result keeps
    every executed tool visible, in order.

    The caller must pass the already-resolved (post skill-load override) tool
    name, and must have skipped tools that intentionally render no card.
    """
    if not tool_id or tool_id in displayed_tools:
        return None
    displayed_tools.add(tool_id)
    display_name = (
        "加载技能"
        if tool_name == "load_skill"
        else display_names.TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
    )
    return {
        "type": "tool_call",
        "tool_name": tool_name,
        "tool_display_name": display_name,
        "tool_args": {},
        "input": {},
        "tool_id": tool_id,
    }


async def _anext_in_subagent_log_scope(iterator, subagent_log_id: str):  # noqa: ANN001
    """Advance one agent-stream item without leaking log tokens across SSE yields."""

    with log_context.subagent_scope(subagent_log_id, source="subagent"):
        return await anext(iterator)


def _chat_has_live_job(chat_id: str) -> bool:
    """这个会话还有在跑（或待启动）的后台作业吗 —— 决定计划栏本轮该不该收尾。"""
    try:
        from core.db.engine import SessionLocal as _JobSession
        from core.db.models import Job as _Job

        with _JobSession() as _db:
            return (
                _db.query(_Job.job_id)
                .filter(_Job.chat_id == chat_id, _Job.status.in_(("pending", "running")))
                .first()
                is not None
            )
    except Exception:  # noqa: BLE001 —— 查不到就当没有作业，照常收尾，别把计划栏永远挂着
        return False
