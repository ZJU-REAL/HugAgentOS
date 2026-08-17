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
    """抓取指定网页 URL 的内容并提取正文。

    何时调我: ① 搜索类技能的 SKILL.md 指令显式要求调用; ② 用户明确要求"抓取/爬取/
    打开"某个具体 URL 并提取正文。用户没给 URL 也没在执行搜索技能时不要自行抓网页,
    找资料先走检索工具。internet_search 返回搜索结果列表（标题+摘要+url）, 我返回单个
    页面正文——通常"先 internet_search 拿 url, 再 web_fetch 取正文"两步走。

    Args:
        url: 要抓取的网页 URL。
        extractMode: "text"（默认，纯文本）/ "markdown"（保留结构）/ "html"（原始 HTML）。
        maxChars: 最大返回字符数（超出截断），默认 50000。

    Returns:
        dict: {"result": extracted_content} 或 {"error": "...", "result": ""}
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
