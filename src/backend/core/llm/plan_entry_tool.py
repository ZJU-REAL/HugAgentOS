"""enter_plan_mode tool — lets the main agent proactively switch into "plan mode" when it judges a task complex enough.

Unlike call_subagent, this tool **executes no task itself**; it is only a
*signal*: when a request is complex enough to be worth producing a plan first and
executing after user confirmation, the LLM calls it. When
orchestration/workflow.py detects the tool call, it emits a ``plan_redirect`` SSE
event (carrying the task description) and aborts the current agent loop
(isomorphic to batch_plan's human-in-the-loop gate). The frontend then drives the
**existing** plan-mode pipeline: generate plan → preview card → user confirmation
→ execute.

Deliberately kept thin: the human-in-the-loop "confirm" gate and the whole
"plan generation/execution" pipeline are reused as-is, unchanged.
"""

from __future__ import annotations

import logging

from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from agentscope.message import TextBlock

logger = logging.getLogger(__name__)


def register_enter_plan_tool(toolkit: Toolkit) -> None:
    """Register the enter_plan_mode tool into the main agent's toolkit.

    The tool body is extremely thin — validate the input and return a friendly
    message; the real "emit plan_redirect event + abort the turn" is handled by
    orchestration/workflow.py based on the tool name and arguments.
    """

    async def enter_plan_mode(task_description: str, reason: str = "") -> ToolResponse:
        """将当前复杂任务转入「计划模式」：先制定分步执行计划、经用户确认后再执行。

        仅当任务确实复杂、值得先规划再动手时才调用——例如需要多个步骤、跨多个
        文件/工具、或属于长时间的工程化任务，且预先给出计划并让用户确认能显著
        降低返工风险。调用后系统会据 task_description 生成一份分步计划并展示给
        用户确认，你**无需**也**不应**继续自行执行该任务——交出控制权、结束本轮即可。

        不要用于：简单问答、单步操作、检索类请求，或你已能直接完成的任务。
        一轮对话最多调用一次。

        Args:
            task_description (`str`):
                要制定计划的完整任务描述。应是自包含、清晰的目标陈述，供计划器
                据此拆解步骤（说明要达成什么、已知的关键约束与背景）。
            reason (`str`):
                （可选）你判断该任务需要进入计划模式的简短理由。

        Returns:
            `ToolResponse`:
                确认已转入计划模式的提示。计划的生成与执行由系统接管。
        """
        task = (task_description or "").strip()
        if not task:
            return ToolResponse(content=[TextBlock(
                type="text",
                text="错误：task_description 不能为空，请提供要制定计划的任务描述。",
            )])
        logger.info(
            "[enter_plan_mode] triggered (task_len=%d, reason=%s)",
            len(task), (reason or "")[:80],
        )
        return ToolResponse(content=[TextBlock(
            type="text",
            text=(
                "已转入计划模式：系统正在根据该任务生成分步执行计划，"
                "将展示给用户在计划卡片上确认后再执行。你无需继续自行处理该任务，本轮到此结束。"
            ),
        )])

    toolkit.register_tool_function(enter_plan_mode, namesake_strategy="skip")


def build_enter_plan_prompt_section() -> str:
    """System-prompt fragment describing the enter_plan_mode tool (stable across turns, prefix-cache friendly)."""
    return (
        "## 计划模式（enter_plan_mode）\n\n"
        "当用户的请求属于**复杂、多步骤、跨多文件/工具或工程化的长任务**，且预先"
        "制定计划并经用户确认能明显降低返工风险时，调用 `enter_plan_mode` 工具，"
        "把任务转入计划模式：系统会据你给出的 task_description 生成一份分步计划、"
        "展示给用户确认后再执行。\n"
        "- 调用后**立即结束本轮**，不要再自行执行该任务，交由计划模式接管。\n"
        "- 简单问答、单步操作、检索类或你已能直接完成的请求**不要**使用；不确定时优先自己处理。\n"
        "- 一轮对话最多调用一次。\n"
    )
