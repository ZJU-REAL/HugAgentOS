"""update_plan tool —— 主智能体的分步计划清单。

模型在同一轮里持续维护清单，工具本身不打断执行；workflow.py 拦截调用并发出
``plan_update`` SSE 事件，前端渲染成输入框上方的计划栏。
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
    """把原始 update_plan 参数规整成 plan_update 事件载荷。

    返回 ``{"title": str, "steps": [{"title", "status"}]}``；参数不完整或非法时返回
    None（流式传参可能只到一半）。工具体与 workflow.py 的 SSE 拦截共用，保证两边对
    "什么算一份有效计划"的判断一致。

    ``explanation`` 只用于约束模型、不进载荷：计划栏渲染的是 title 与 steps，把改动
    理由混进去会污染用户可见的标题栏。
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
    """把 update_plan 注册进主智能体的工具集。

    工具体只校验并回报计数，用户可见的计划栏由 workflow.py 依据调用参数驱动。描述里
    保留步骤推进的节奏说明：它贴着模型做工具选择的那一刻，而系统提示词那一节躺在两万多
    token 的静态前缀里——2026-08-14 压缩 prefill 删掉后，长任务里模型开头调两次就再也
    不更新（commit 7578469f）。
    """

    async def update_plan(steps: list, title: str = "", explanation: str = "") -> ToolResponse:
        """更新任务计划。传入一份计划项列表，每项含步骤与状态，以及可选的说明。
        至多一个步骤可以处于 in_progress。调用本工具**不会打断**执行。

        做下一步之前先把已完成的步骤标成 completed；如果一遍就把多个步骤做完了，直接
        一次性全部标成 completed。只在步骤状态确实发生变化时才调用：和上次提交的清单
        完全相同就不要再调，重复提交同一份清单没有任何作用。所有步骤都标成 completed
        之后计划即告终结，不要再调用本工具，直接输出最终文字回复。

        Args:
            steps (`list`):
                完整的步骤列表（每次调用都传**全量列表**，不是增量）。每个元素为
                ``{"title": "步骤标题", "status": "pending|in_progress|completed"}``。
            title (`str`):
                （可选）计划标题，简洁概括任务目标。会显示给用户，不要写进改动理由。
            explanation (`str`):
                （可选）本次改动的理由。中途增删步骤、重写计划结构时必须填写。

        Returns:
            `ToolResponse`:
                当前清单的各状态计数。
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
        counts = {s: 0 for s in _VALID_STATUSES}
        for step in parsed["steps"]:
            counts[step["status"]] += 1
        logger.info("[update_plan] %d/%d steps completed (title=%s, why=%s)",
                    counts["completed"], len(parsed["steps"]),
                    parsed["title"][:60], (explanation or "")[:60])
        checklist = "\n".join(
            f"  {i}. [{s['status']}] {s['title']}"
            for i, s in enumerate(parsed["steps"], start=1)
        )
        text = (
            f"计划已更新：{counts['pending']} 待办，"
            f"{counts['in_progress']} 进行中，{counts['completed']} 已完成。\n"
            f"当前清单（这是计划栏的权威状态，以此为准）：\n{checklist}"
        )
        if counts["completed"] == len(parsed["steps"]):
            text += (
                "\n全部步骤已完成，计划终结：不要再调用 update_plan，也不要原样重复"
                "任何已成功的工具调用，请直接输出面向用户的最终文字回复。"
            )
        return ToolResponse(content=[TextBlock(type="text", text=text)])

    toolkit.register_tool_function(update_plan, namesake_strategy="skip")


def build_plan_update_prompt_section() -> str:
    """系统提示词里的计划清单一节（跨轮稳定，对前缀缓存友好）。

    正文的真源是 ``prompts/prompt_text/plan_tool/plan_tool.system.md``，运行时取
    ``plan_tool`` 版本池的激活版本，可在 Config 管理台「任务计划清单」里编辑。
    """
    from core.services import prompt_version_service as pvs

    return pvs.render_kind_segment("plan_tool")
