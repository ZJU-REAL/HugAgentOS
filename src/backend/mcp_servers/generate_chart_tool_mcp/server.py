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

    try:
        from .chart import generate_chart_tool as _tool
    except ImportError as exc:
        # matplotlib/Pillow 只在 mcp 容器与桌面本机安装器里显式声明；纯 dev venv
        # （requirements.txt）没有它们。缺依赖时端口照常健康，这里把 ImportError
        # 转成结构化错误，避免向模型甩原始 traceback。
        return {
            "ok": False,
            "error": (
                "图表依赖未安装（matplotlib / Pillow），无法生成图表。"
                "请在本服务的运行环境执行 pip install -r docker/requirements-mcp.txt 后重试。"
                f"缺失详情：{exc}"
            ),
        }

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
    # 启动即探测绘图依赖：缺了照常起服务（工具调用时返回结构化错误），但在
    # server.log 里留一行警告，避免"端口健康、调用必炸"却无迹可查。
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print(
            "[generate_chart_tool_mcp] warning: matplotlib 未安装，"
            "generate_chart_tool 将不可用（pip install -r docker/requirements-mcp.txt）",
            file=sys.stderr,
        )
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9104)


if __name__ == "__main__":
    main()
