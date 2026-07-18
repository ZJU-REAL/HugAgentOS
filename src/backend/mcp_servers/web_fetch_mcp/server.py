#!/usr/bin/env python3
"""stdio MCP server exposing tool: web_fetch."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from typing import Any, Dict

from mcp.server import FastMCP

mcp = FastMCP("hugagent-web-fetch")


@mcp.tool()
async def web_fetch(
    url: str = "",
    extractMode: str = "text",
    maxChars: int = 50000,
) -> Dict[str, Any]:
    """抓取指定网页 URL 的内容并提取正文。当用户要求“抓取”、“爬取”网页内容时，可以使用该工具进行网站数据抓取和正文提取。

    适用场景：
    - 需要获取某个网页的正文内容进行分析或总结。
    - 搭配搜索引擎结果，抓取具体页面详情。
    - 提取网页中的关键信息（文本、Markdown 或原始 HTML）。

    使用建议：
    - extractMode="text" 适合纯文本提取（默认）。
    - extractMode="markdown" 保留标题、链接、列表等结构。
    - extractMode="html" 返回原始 HTML，适合需要精确解析的场景。
    - maxChars 控制返回内容长度，避免过长影响后续处理。

    Args:
        url: 要抓取的网页 URL。
        extractMode: 提取模式，可选 "text"、"markdown"、"html"。
        maxChars: 最大返回字符数（超出部分截断），默认 50000。

    Returns:
        dict: {"result": extracted_content} 或 {"error": "...", "result": ""}

    调用决策（何时使用我）:
    - **何时调我**: ① 由搜索类技能（如"中文网页搜索"）的 SKILL.md 指令显式要求调用;
      ② 用户明确要求"抓取"/"爬取"/"打开"某个具体 URL 并提取正文。
    - **何时不要调我**: 用户没明确给 URL、也没在执行某个搜索技能时, 不要自行去抓网页。
      要找资料先走 retrieve_dataset_content / get_industry_news 等内部工具。
    - 与 internet_search 的取舍: internet_search 返回的是搜索结果列表（标题+摘要+url）;
      我返回的是单个页面的正文。一般是"先 internet_search 拿到 url, 再 web_fetch 取
      正文"的两步流程。
    """
    from mcp_servers.web_fetch_mcp.impl import fetch_url

    # Normalize extractMode
    if isinstance(extractMode, dict):
        payload = extractMode
        url = url or str(payload.get("url", "")).strip()
        extractMode = str(payload.get("extractMode", "text")).strip().lower()
        try:
            maxChars = int(payload.get("maxChars", maxChars))
        except Exception:
            pass

    mode = str(extractMode).strip().lower()
    if mode not in {"text", "markdown", "html"}:
        mode = "text"

    if not url.strip():
        return {
            "error": "web_fetch 缺少 url 参数",
            "result": "",
        }

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            result = await fetch_url(
                url=url.strip(),
                extract_mode=mode,
                max_chars=max(1, maxChars),
            )
    except Exception as e:
        logs = buf.getvalue().strip()
        if logs:
            print(logs, file=sys.stderr)
        return {
            "error": f"web_fetch 调用失败: {e}",
            "result": "",
        }

    logs = buf.getvalue().strip()
    if logs:
        print(logs, file=sys.stderr)

    return {"result": result}


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9106)


if __name__ == "__main__":
    main()
