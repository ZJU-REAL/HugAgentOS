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
        完全相同就不要再调，重复提交同一份清单没有任何作用。

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
        return ToolResponse(content=[TextBlock(
            type="text",
            text=(
                f"计划已更新：{counts['pending']} 待办，"
                f"{counts['in_progress']} 进行中，{counts['completed']} 已完成。\n"
                f"当前清单（这是计划栏的权威状态，以此为准）：\n{checklist}"
            ),
        )])

    toolkit.register_tool_function(update_plan, namesake_strategy="skip")


def build_plan_update_prompt_section() -> str:
    """系统提示词里的计划清单一节（跨轮稳定，对前缀缓存友好）。"""
    return (
        "## 任务计划清单（update_plan）\n\n"
        "你可以使用 `update_plan` 工具追踪步骤与进度，并把它渲染给用户。用这个工具能表明"
        "你已经理解了任务，也让用户看清你打算怎么做。计划能让复杂、有歧义或多阶段的工作"
        "对用户更清楚、更可协作。一份好的计划把任务拆成有意义、有先后逻辑、且随做随能"
        "核验的步骤。\n"
        "注意计划不是用无关紧要的步骤给简单工作凑数，也不是把显而易见的事说一遍。计划内容"
        "不应包含你根本做不到的事（比如别去测你没法测的东西）。对于你能直接做完或直接回答"
        "的简单、单步请求，不要使用计划。\n"
        "调用 `update_plan` 之后**不要**在回复正文里重复罗列整份计划——计划栏已经展示给"
        "用户了。只说明这次改了什么，并点出重要的上下文或下一步。\n"
        "执行下一条命令之前，先想一想上一步是不是已经做完了，做完就先把它标成 completed "
        "再往下走。只在步骤状态确实发生变化时才调用：和上次提交的清单完全相同就不要再调，"
        "重复提交同一份清单没有任何作用，也不会让计划栏更新。也可能一遍实现就把计划里所有"
        "步骤都做完了——如果是这样，直接把所有步骤一次性标成 completed 即可。有时任务中途"
        "需要改计划：照常调用 `update_plan` 传入"
        "新的全量列表，并在 `explanation` 里说明改动理由。始终保持至多一个步骤处于 "
        "in_progress。\n\n"
        "**什么时候用计划**：\n"
        "- 任务不简单，需要在较长时间跨度上执行多个动作；\n"
        "- 存在阶段划分或依赖关系，先后顺序很重要；\n"
        "- 任务有歧义，先列出高层目标会更清楚；\n"
        "- 你希望有中间检查点，便于反馈与验证；\n"
        "- 用户在一条消息里要求了不止一件事；\n"
        "- 用户明确要求你使用计划工具（也叫「待办清单」）；\n"
        "- 你在执行过程中又产生了新步骤，并打算在交还给用户之前做完它们。\n\n"
        "**示例**\n"
        "高质量计划：\n"
        "- 例 1：1) 新增带文件参数的命令行入口 2) 用 CommonMark 库解析 Markdown "
        "3) 套用语义化 HTML 模板 4) 处理代码块、图片与链接 5) 为非法文件补错误处理\n"
        "- 例 2：1) 定义颜色的 CSS 变量 2) 加入带 localStorage 状态的切换开关 "
        "3) 改造组件改用变量 4) 逐个视图检查可读性 5) 加上主题切换过渡动画\n"
        "低质量计划：\n"
        "- 例 1：1) 做个命令行工具 2) 加个 Markdown 解析器 3) 转成 HTML\n"
        "- 例 2：1) 加暗色模式开关 2) 保存偏好 3) 把样式弄好看\n"
        "如果要写计划，只写高质量的那种，不要写低质量的那种。\n"
    )
