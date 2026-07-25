"""update_plan tool — lightweight in-conversation plan/progress tracker for the main agent.

Codex-style ``update_plan``: for complex multi-step tasks the main agent
maintains a step checklist while it keeps working **in the same turn** — the
tool never interrupts or redirects the conversation. orchestration/workflow.py
intercepts the tool call and emits a ``plan_update`` SSE event; the frontend
renders it as a slim plan bar above the chat input (NOT as a chat message).

This replaces the old ``enter_plan_mode`` redirect for model-initiated
planning: no turn abort, no approval gate, no separate execution pipeline.
The manual plan mode (user-toggled, generate → approve → execute) is a
separate pipeline and is unaffected.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from agentscope.message import TextBlock

logger = logging.getLogger(__name__)

_VALID_STATUSES = ("pending", "in_progress", "completed")


def parse_plan_update_args(tool_args: Any) -> Optional[Dict[str, Any]]:
    """Normalize raw update_plan tool args into a plan_update event payload.

    Returns ``{"title": str, "steps": [{"title", "status"}]}`` or None when the
    args are not yet complete/valid (streaming may deliver partial args).
    Shared by the tool body and workflow.py's SSE interception so both sides
    agree on what counts as a valid plan.
    """
    if not isinstance(tool_args, dict):
        return None
    raw_steps = tool_args.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps: List[Dict[str, str]] = []
    for s in raw_steps:
        if isinstance(s, str):
            s = {"title": s}
        if not isinstance(s, dict):
            return None
        step_title = str(s.get("title") or s.get("step") or "").strip()
        if not step_title:
            return None
        status = str(s.get("status") or "pending").strip().lower()
        if status not in _VALID_STATUSES:
            status = "pending"
        steps.append({"title": step_title, "status": status})
    return {
        "title": str(tool_args.get("title") or "").strip(),
        "steps": steps,
    }


def register_plan_update_tool(toolkit: Toolkit) -> None:
    """Register the update_plan tool into the main agent's toolkit.

    The tool body only validates and echoes progress back to the LLM; the
    user-facing side effect (the plan bar) is driven by workflow.py emitting
    ``plan_update`` from the tool-call arguments.
    """

    async def update_plan(steps: list, title: str = "") -> ToolResponse:
        """维护当前复杂任务的分步计划清单（展示在用户输入框上方的计划栏）。

        面对复杂、多步骤的任务时用它列出并更新执行计划：开始动手前先调用一次
        列出全部步骤；每完成一步立即再次调用，更新各步骤的 status。调用本工具
        **不会打断执行**——更新完计划后继续在本轮对话中正常执行任务即可。

        Args:
            steps (`list`):
                完整的步骤列表（每次调用都传全量列表，不是增量）。每个元素为
                ``{"title": "步骤标题", "status": "pending|in_progress|completed"}``。
                保持恰好一个步骤处于 in_progress；已完成的标记 completed。
            title (`str`):
                （可选）计划标题，简洁概括任务目标。

        Returns:
            `ToolResponse`:
                当前进度确认。继续执行任务，不要停下等待。
        """
        parsed = parse_plan_update_args({"title": title, "steps": steps})
        if not parsed:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=(
                    "错误：steps 必须是非空列表，每个元素为 "
                    '{"title": "...", "status": "pending|in_progress|completed"}。'
                ),
            )])
        done = sum(1 for s in parsed["steps"] if s["status"] == "completed")
        total = len(parsed["steps"])
        logger.info("[update_plan] %d/%d steps completed (title=%s)",
                    done, total, parsed["title"][:60])
        return ToolResponse(content=[TextBlock(
            type="text",
            text=f"计划已更新（{done}/{total} 步完成）。请继续执行任务。",
        )])

    toolkit.register_tool_function(update_plan, namesake_strategy="skip")


def build_plan_update_prompt_section() -> str:
    """System-prompt fragment describing the update_plan tool (stable across turns, prefix-cache friendly)."""
    return (
        "## 任务计划清单（update_plan）\n\n"
        "面对**复杂、多步骤**的任务（预计需要多次工具调用、跨多个文件/成果物、"
        "或需要较长执行过程）时，用 `update_plan` 工具维护一份分步计划清单，"
        "它会展示在用户输入框上方，让用户随时看到你的整体计划与进度：\n"
        "- 开始动手前先调用一次，列出 3-8 个步骤（第一步 in_progress，其余 pending）；\n"
        "- 每完成一步**立即**再调用一次，传入全量步骤列表并更新各步 status，"
        "保持恰好一个步骤处于 in_progress；\n"
        "- 计划有变（需要增删/合并步骤）时同样通过 update_plan 更新全量列表；\n"
        "- 对有可核验产出的任务（生成文件/代码/数据处理等），在计划末尾加一个"
        "「验证与修正」步骤：核验产物是否达标，发现问题**直接修复并复验**，而不是只报告；\n"
        "- 简单问答、单步操作、检索类请求**不要**使用。\n"
        "调用 update_plan 不会打断你——更新后继续在本轮对话里正常执行任务。\n"
    )
