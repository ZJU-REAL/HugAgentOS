"""Public internet search MCP tool."""

from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .impl import MAX_INFLIGHT, SearchService
from .models import Depth, Query, ResultLimit, SearchRequest, Topic

# FastMCP enters lifespan per MCP session. Share one service across overlapping
# sessions on this server's event loop; the last session closes its client.
_service: SearchService | None = None
_sessions = 0


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _service, _sessions
    if _service is None:
        _service = SearchService(
            httpx.AsyncClient(limits=httpx.Limits(max_connections=MAX_INFLIGHT))
        )
    service = _service
    _sessions += 1
    try:
        yield service
    finally:
        _sessions -= 1
        if _sessions == 0:
            _service = None
            with anyio.CancelScope(shield=True):
                await service.client.aclose()


class SearchMCP(FastMCP):
    """Validate the raw MCP boundary before FastMCP drops unknown arguments."""

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if name == "internet_search":
            SearchRequest.model_validate(arguments)
        return await super().call_tool(name, arguments)


mcp = SearchMCP("hugagent-internet-search", lifespan=lifespan)


@mcp.tool()
async def internet_search(
    ctx: Context,
    query: Query = "",
    max_results: ResultLimit = 5,
    topic: Topic = "general",
    search_depth: Depth = "advanced",
    include_raw_content: Annotated[bool, Field(strict=True)] = False,
    queries: list[Query] | None = None,
) -> dict[str, Any]:
    """检索公开互联网资料、最新信息、官方文档与技术资料，可直接使用。

    内部制度和业务记录优先对应内部权威来源，外部资料只作补充。
    简单问题传 query；复杂调研传 2–4 个互补 queries，max_results=8。
    query 与 queries 二选一；按查询顺序轮流选取、按 URL 去重，最终最多 8 条。
    可使用英文技术关键词，回答语言遵循用户要求，不限制来源语言。
    search_depth 控制服务商检索深度，不代表完成多轮研究。
    返回日期、内容截断、结果数量限制及各查询状态；部分失败保留成功来源。
    """
    request = SearchRequest(
        query=query,
        queries=queries,
        max_results=max_results,
        topic=topic,
        search_depth=search_depth,
        include_raw_content=include_raw_content,
    )
    service: SearchService = ctx.request_context.lifespan_context
    result = await service.search(request)
    if result["status"] == "error":
        return {"error": "All search queries failed", "result": result}
    return {"result": result}


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9102)


if __name__ == "__main__":
    main()
