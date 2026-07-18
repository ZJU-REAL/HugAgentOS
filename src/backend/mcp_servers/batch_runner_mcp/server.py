#!/usr/bin/env python3
"""stdio MCP server exposing the batch_plan tool.

This tool is the LLM's entry point into the batch-execution flow. It
*generates a plan* (does NOT execute it) and returns a plan_id. The
backend then pauses the agent stream, asks the user to confirm via UI,
and only after confirmation does BatchOrchestrator iterate over the items.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from mcp.server import FastMCP

mcp = FastMCP("hugagent-batch-runner")


@mcp.tool()
async def batch_plan(
    instruction: str = "",
    file_ids: List[str] = [],
    text_items: List[str] = [],
    chat_id: str = "",
) -> Dict[str, Any]:
    """批量执行调度器（必读）：把"对一组对象逐个做同一件事"打包成可确认的执行计划。

    ⚠️ **强制规则**：当用户消息包含"批量"、"分别"、"逐个"、"每一个"、"挨个"、
    "依次"、"一个个"、"分别给出"、"分别分析"、"分别处理"、"对每个 X"、
    "对这些 X"、"针对每一项"、"对以下 N 个" 等任意一个表达，**或** 用户在
    一句话里**枚举了 ≥2 个并列的对象**（公司、城市、文件、主题、人名、产品等），
    **或** 用户明确给出了 N（"3 家"、"5 个"、"这 10 份"）—— 你**必须**调用本
    工具，**禁止**自己直接回答。

    ✅ **典型触发场景**（看到任何一个就该调本工具）：
      • "请分别用一句话评价阿里、腾讯、字节" → 三个对象 → 调用本工具
      • "对这 5 家公司给出经营建议" → 5 个对象 → 调用本工具
      • "上传了一个 Excel，对每行的公司做分析" → xlsx → 调用本工具
      • "这是 3 份合同，逐份提取关键条款" → multi word → 调用本工具
      • "分别介绍北京、上海、深圳" → 3 个对象 → 调用本工具

    ❌ **不要使用的场景**：
      • 单一对象的问答（"介绍下阿里巴巴"）
      • 问知识、概念、规则（"什么是产业链？"）
      • 单文档总结（"总结这份报告"）

    **如何使用：**
    - 自然语言枚举 → `text_items` 传入对象数组（如 ["阿里","腾讯","字节"]）
    - 上传的文件 → `file_ids` 传入文件 id 列表（从聊天上下文中获取）
    - `instruction` 用一句话陈述对每一项要做什么（如"用一句话评价"）

    **关键行为：调用本工具后立即停止当前回合，不要再输出任何文字、不要再调用
    其他工具。** 系统会暂停 SSE 流，弹出确认对话框让用户审阅/修改 prompt 模板，
    用户点确认后**后端会自动逐条执行并把结果实时推送给用户**——你完全不需要
    自己循环处理每一项，也不要重复调用本工具。

    Args:
        instruction: 一句话陈述对每一项要做什么（必填），例如"用一句话评价该公司"。
        file_ids: 用户上传的文件 id 列表（按文件批量处理时使用）。
        text_items: 用户在文本里枚举的对象列表（按文本批量处理时使用，最常见）。
        chat_id: 当前会话 id（如能从上下文获取则传入，便于关联 plan 与用户）。

    Returns:
        计划摘要 dict：
        {
          "plan_id": str,                 # 后续确认 + 执行用此 id
          "total": int,                   # 计划中的项目总数
          "preview": [{...}, ...],        # 前 3 条 item 预览
          "source_type": str,             # xlsx | word_files | text_list
          "default_template": str,        # 推断出的默认 prompt 模板（用户可改）
          "placeholder_keys": [str, ...], # 模板中可用的占位符字段名
          "status": "pending"             # 等待用户确认
        }

    返回后**立即结束本回合**，等待用户确认。
    """
    from mcp_servers.batch_runner_mcp._planner import create_plan

    return await create_plan(
        instruction=instruction or "",
        file_ids=file_ids or [],
        text_items=text_items or [],
        chat_id=chat_id or "",
    )


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9107)


if __name__ == "__main__":
    main()
