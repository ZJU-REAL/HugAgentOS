#!/usr/bin/env python3
"""stdio MCP server exposing tool: export_table_to_excel.

Note: ``export_report_to_docx`` 的 MCP 工具入口已下线（由 word-editing skill 的
``word-cli create --markdown`` 取代——markdown 引擎已经搬进该 skill 自带的
``scripts/engine/markdown_engine.py``）。函数体保留，仍由
selftest 与 ``tests/report_export_fallback_selftest.py`` 直接调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from typing import Any, Dict, Optional

from mcp.server import FastMCP

mcp = FastMCP("hugagent-report-export")


# 已下线：不再以 MCP 工具形式暴露。请使用 word-editing skill 的
# `word-cli create --markdown ...`。函数本体保留作为 selftest / fallback 测试的可调用入口。
async def export_report_to_docx(
    markdown: str,
    title: str = "报告",
    filename: Optional[str] = None,
    language: str = "zh",
) -> Dict[str, Any]:
    """
    [DEPRECATED MCP TOOL — superseded by word-editing skill `word-cli create`]

    ⚡ Lightweight export: convert an EXISTING Markdown string into a .docx download artifact.

    USE WHEN: the user wants to download a Markdown report (already generated in this chat)
              as a Word file. Headings use 方正小标宋简体, body uses 方正仿宋简体 (公文字体).
              Typical requests: "把刚才的分析导出为 Word"、"生成这份报告的 docx 下载"。

    DO NOT USE WHEN: the user needs custom styles, multi-section layout, headers/footers,
                    TOC, image insertion, template fill, or editing of an existing .docx.
                    → Use the skill instead (more powerful, template-aware).

    Args:
        markdown: The Markdown source text (required).
        title:    Document title shown as the top heading. Default "报告".
        filename: Optional output filename. Auto-generated if omitted.
        language: "zh" (default) or "en" — affects font selection.

    Returns: {"ok": true, "file_id": "...", "url": "/files/...", "name": "xxx.docx",
              "size": 12345, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    """
    from mcp_servers.report_export_mcp.impl import export_report_to_docx as _impl

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = _impl(markdown=markdown, title=title, filename=filename, language=language)

    logs = buf.getvalue().strip()
    if logs:
        print(logs, file=sys.stderr)

    if isinstance(result, dict):
        return result
    return {"ok": False, "error": "unexpected export result"}


@mcp.tool()
async def export_table_to_excel(
    markdown: str,
    title: str = "表格",
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ⚡ Lightweight export: parse Markdown table(s) and convert into an Excel (.xlsx) download.

    USE WHEN: the user has Markdown tables (standard `| col | col |` format, already generated
              in this chat) and wants a quick Excel download. Headers auto-detected from the
              row preceding `|---|---|`. Basic styling applied (header row, alternating rows,
              borders). Each Markdown table becomes one sheet.
              Typical requests: "把这张表下载为 Excel"、"导出上面的表格为 xlsx"。

    DO NOT USE WHEN: the user needs formulas, cross-sheet references, multi-sheet financial
                    models, pivot tables, role-based styling (input/formula/header coloring),
                    formula validation/repair, or editing an existing .xlsx.
                    → Use the skill instead (Formula-First, full pipeline support).

    Args:
        markdown: Markdown text containing one or more tables (required).
        title:    Default sheet title if a table has no heading. Default "表格".
        filename: Optional output filename. Auto-generated if omitted.

    Returns: {"ok": true, "name": "xxx.xlsx",
              "size": 12345, "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              "sheet_count": 1, "note": "表格已生成，下载信息由系统在附件区处理"}

    Example input (markdown):
        | 月份 | 销量 | 利润 |
        |------|------|------|
        | 1月  | 100  | 30   |
        | 2月  | 150  | 45   |

    调用决策（何时使用我）:
    - **何时调我**: 用户要求把"对话中已有的 Markdown 表格"导出为 Excel/xlsx 下载,
      典型说法: "把这张表下载为 Excel" / "导出上面的表格为 xlsx"。
    - **何时不要调我**: ① 需要公式、跨 sheet 引用、多 sheet 财务模型、数据透视、
      角色化样式、公式校验/修复、或编辑已有 .xlsx → 改走 excel-editing skill 的
      `excel-cli create / edit` 子命令。② 用户要的是 Word 报表而非表格 → 走
      word-editing skill (`word-cli create / edit`)。
    - 与 excel-editing 技能的取舍: 我是"轻量一键下载", 不支持公式/编辑;
      excel-editing 是完整 Excel 操作能力。一次性导出走我, 需要继续编辑/算公式
      走 `excel-cli`。
    """
    from mcp_servers.report_export_mcp.impl import export_table_to_excel as _impl

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = _impl(markdown=markdown, title=title, filename=filename)

    logs = buf.getvalue().strip()
    if logs:
        print(logs, file=sys.stderr)

    if isinstance(result, dict):
        if result.get("ok"):
            payload = dict(result)
            payload.setdefault("note", "表格已生成，可在附件区查看或下载")
            return payload
        return result
    return {"ok": False, "error": "unexpected export result"}


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9105)


if __name__ == "__main__":
    main()
