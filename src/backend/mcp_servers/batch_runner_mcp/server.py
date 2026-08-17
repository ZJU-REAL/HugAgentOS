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
    """批量执行调度器：把"对一组对象逐个做同一件事"打包成可确认的执行计划（只生成计划，不执行）。

    ⚠️ **强制规则**：用户消息包含"批量/分别/逐个/每一个/挨个/依次/一个个/对每个 X/
    针对每一项/对以下 N 个"等任一表达，**或**一句话里枚举了 ≥2 个并列对象（公司、
    城市、文件、主题等），**或**明确给出数量（"3 家"、"这 10 份"）——**必须**调用
    本工具，**禁止**自己直接回答。如"请分别用一句话评价阿里、腾讯、字节"→ 调用；
    上传 Excel 要求对每行做分析 → 调用。
    不适用：单一对象问答、概念解释、单文档总结。

    用法：`text_items` 传枚举的对象数组（最常见）；`file_ids` 传上传文件 id 列表；
    `instruction` 一句话陈述对每一项做什么。

    **关键行为：调用本工具后立即结束本回合，不要再输出任何文字、不要再调用其他
    工具。** 系统会弹出确认对话框，用户确认后后端自动逐条执行并实时推送结果——
    不要自己循环处理每一项，也不要重复调用本工具。

    Args:
        instruction: 对每一项要做什么（必填）。
        file_ids: 上传文件 id 列表。
        text_items: 文本枚举的对象列表。
        chat_id: 当前会话 id（能从上下文获取则传入）。

    Returns:
        计划摘要 dict：{"plan_id", "total", "preview", "source_type",
        "default_template", "placeholder_keys", "status": "pending"}。
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
