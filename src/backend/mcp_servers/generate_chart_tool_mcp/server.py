#!/usr/bin/env python3
"""stdio MCP server exposing tool: generate_chart_tool."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from typing import Any, Dict

from mcp.server import FastMCP

mcp = FastMCP("hugagent-generate-chart-tool")


@mcp.tool()
async def generate_chart_tool(data: str, query: str) -> Dict[str, Any]:
    """根据给定数据生成可视化图表（matplotlib），将图片保存到存储并返回结果摘要。

    适用场景：
    - 用户明确要求：画图/绘图/生成图表（折线图、柱状图、饼图等）。

    调用规范（严禁跳过）：
    - **禁止凭空绘图**：必须先通过数据查询工具获取真实数据。
    - 将数据整理为 JSON 字符串传入 data；在 query 中写清：图表类型、标题、坐标轴、单位换算要求等。

    Args:
        data: 绘图数据（JSON 字符串）。例如：{"年份":[2022,2023],"增加值":[123,145]}。
        query: 绘图指令。例如："画折线图，标题为xxx，单位换算为亿元"。

    Returns:
        dict: {"ok": true, "file_id": "<artifact id>", "url": "/files/<file_id>",
               "name": "chart_xxx.png", "size": 12345, "mime_type": "image/png",
               "note": "..."}
              或失败时: {"ok": false, "error": "..."}

        **关键**：图表被保存为一条 artifact（用 `file_id` 标识），它在附件区可下载，
        但**不在沙盒里**。要把它插进 Word / PPT 等沙盒产物，必须先把这个 `file_id`
        拷进沙盒，再让 CLI 引用沙盒里的路径：
          1. `sandbox_put_artifact(artifact_id=<本工具返回的 file_id>,
                                   dest_path="/workspace/chart1.png")`
          2. `word-cli edit … --ops '[{"op":"insert_image",
                 "image_path":"/workspace/chart1.png", "anchor":"表2",
                 "position":"after", "width_cm":14}]'`
        不要把 `file_id` 直接当成沙盒路径传给 CLI——CLI 在沙盒里跑，解析不了 artifact id。

    调用决策（何时使用我）:
    - **何时调我**: 用户明确要求"画图 / 绘图 / 生成图表 / 折线图 / 柱状图 / 饼图"
      等可视化产物时。
    - **强制前置步骤**: 必须先通过用户提供的数据、知识库检索结果或已启用的
      指标类工具/技能拿到真实数值, 再用我画。**禁止凭空绘图、禁止编造数据**。
    - 不要用我: 用户问的是"分析/对比/趋势文字描述"而非图表; 或者还没有任何数据时。
    """

    from .chart import generate_chart_tool as _tool

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = await _tool(data=data, query=query)

    logs = buf.getvalue().strip()
    if logs:
        print(logs, file=sys.stderr)

    if isinstance(result, dict):
        if result.get("ok"):
            payload = dict(result)
            payload.setdefault("note", "图表已生成，可在附件区查看或下载")
            return payload
        return result
    return {"result": result}


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9104)


if __name__ == "__main__":
    main()
