#!/usr/bin/env python3
"""stdio MCP server exposing tool: internet_search."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from typing import Any, Dict

from mcp.server import FastMCP

mcp = FastMCP("hugagent-internet-search")


@mcp.tool()
async def internet_search(
    query: str = "",
    max_results: int = 5,
    topic: Any = "general",
    search_depth: Any = "advanced",
    include_raw_content: bool = False,
    cn_only: bool = True,
) -> Dict[str, Any]:
    """互联网检索（兜底工具）。

    适用场景：
    - 当内部数据源无法提供足够信息时，用于补充公开网页/新闻等外部信息。

    使用建议：
    - 优先让查询更具体（带时间、地区、实体名）。
    - 尽量只在必要时使用，避免用互联网信息替代内部权威数据。

    Args:
        query: 搜索关键词/问题。
        max_results: 返回条数。
        topic: general/news/finance。
        search_depth: basic/advanced/fast/ultra-fast。
        include_raw_content: 是否包含原始内容。
        cn_only: 是否仅返回中文结果（默认 true）。

    Returns:
        dict: {"result": tavily_search_result}

    调用决策（何时使用我）:
    - **优先级**: 兜底（4-最低）。**仅当**内部知识库、私有知识库、产业链工具、
      产业资讯工具、AI 动态工具和 Agent Skills 中的搜索/数据类技能
      都无结果时, 才作为最后兜底调我。
    - 与搜索类技能的取舍: **凡是搜索类技能能覆盖的场景, 一律走技能, 不走我**。
      技能通过 web_fetch 调用专门搜索引擎 URL, 效果远优于 internet_search。
    - 不要用我: 当问题已能被内部数仓/知识库/产业链工具回答时, 别用互联网信息替代
      内部权威数据。
    """

    from mcp_servers.internet_search_mcp.impl import internet_search as _impl

    # Be tolerant to malformed tool args emitted by LLM (e.g. topic gets a dict payload).
    if isinstance(topic, dict):
        payload = topic
        if not query:
            query = str(payload.get("query", "")).strip()
        try:
            max_results = int(payload.get("max_results", max_results))
        except Exception:
            pass
        topic = payload.get("topic", "general")
        search_depth = payload.get("search_depth", search_depth)
        include_raw_content = bool(payload.get("include_raw_content", include_raw_content))
        cn_only = bool(payload.get("cn_only", cn_only))

    topic_text = str(topic).strip().lower()
    if topic_text not in {"general", "news", "finance"}:
        topic_text = "general"
    search_depth_text = str(search_depth).strip().lower()
    if search_depth_text not in {"basic", "advanced", "fast", "ultra-fast"}:
        search_depth_text = "advanced"

    if not query.strip():
        return {
            "error": "internet_search 缺少 query 参数",
            "result": [],
        }

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            result = _impl(
                query=query,
                max_results=max(1, max_results),
                topic=topic_text,
                search_depth=search_depth_text,
                include_raw_content=include_raw_content,
                cn_only=cn_only,
            )
    except Exception as e:
        logs = buf.getvalue().strip()
        if logs:
            print(logs, file=sys.stderr)
        return {
            "error": f"internet_search 调用失败: {e}",
            "result": [],
        }

    logs = buf.getvalue().strip()
    if logs:
        print(logs, file=sys.stderr)

    return {"result": result}


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9102)


if __name__ == "__main__":
    main()
