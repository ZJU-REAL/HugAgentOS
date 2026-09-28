#!/usr/bin/env python3
"""MCP server exposing tools: retrieve_dataset_content & retrieve_local_kb.

Supports two transports:
- stdio (default): spawned per-request as a subprocess
- streamable-http: long-running HTTP server, runtime params via HTTP headers
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

from .descriptions import (
    _BASE_LOCAL_KB_TOOL_DESCRIPTION,
    _BASE_TOOL_DESCRIPTION,
    _LIST_DATASETS_DESCRIPTION,
    _WIKI_EXPAND_DESCRIPTION,
    _WIKI_FETCH_SOURCE_DESCRIPTION,
    _WIKI_LOCATE_DESCRIPTION,
    _WIKI_OVERVIEW_DESCRIPTION,
    _WIKI_READ_PAGE_DESCRIPTION,
)
from .runtime import (
    BlockingLane,
    read_positive_float_env,
    read_positive_int_env,
    tool_timeout_payload,
)

mcp = FastMCP("hugagent-retrieve-dataset-content")
_LOGGER = logging.getLogger(__name__)


_PRIVATE_KB_TIMEOUT_SECONDS = read_positive_float_env("RETRIEVE_LOCAL_KB_TIMEOUT_SECONDS", 30.0)
_PRIVATE_KB_MAX_WORKERS = read_positive_int_env("RETRIEVE_LOCAL_KB_MAX_WORKERS", 4)
_LIST_DATASETS_TIMEOUT_SECONDS = read_positive_float_env(
    "LIST_KNOWLEDGE_BASES_TIMEOUT_SECONDS", 30.0
)


_PRIVATE_KB_LANE = BlockingLane(name="private-retrieve", max_workers=_PRIVATE_KB_MAX_WORKERS)
_DATASET_LIST_LANE = BlockingLane(name="dataset-list", max_workers=2)


# ── Header names for runtime parameters (HTTP mode) ─────────────────────────
_HDR_ALLOWED_DATASET_IDS = "x-allowed-dataset-ids"
_HDR_ALLOWED_KB_IDS = "x-allowed-kb-ids"
_HDR_CURRENT_USER_ID = "x-current-user-id"
_HDR_RERANKER_ENABLED = "x-reranker-enabled"


def _get_header(ctx: Optional[Context], name: str) -> Optional[str]:
    """Extract an HTTP header from the MCP request context.

    Returns None if ctx is unavailable (stdio mode) or the header is absent.
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
        if request is None:
            return None
        value = request.headers.get(name)
        return value if value else None
    except Exception as exc:
        _LOGGER.warning("_get_header(%s) failed: %s (ctx type=%s)", name, exc, type(ctx))
        return None


@mcp.tool(description=_BASE_TOOL_DESCRIPTION)
async def retrieve_dataset_content(
    query: str,
    dataset_id: str = "",
    top_k: int = 10,
    score_threshold: float = 0.4,
    search_method: str = "hybrid_search",
    reranking_enable: bool = False,
    weights: float = 0.6,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Execute dataset retrieval and return MCP-compatible payload."""

    from mcp_servers.retrieve_dataset_content_mcp.impl import (
        RETRIEVE_TOTAL_TIMEOUT_SECONDS,
        DatasetRetrievalTimeoutError,
        DatasetRetrievalUnavailableError,
        retrieve_dataset_content_async,
    )

    # Read runtime params from HTTP headers (None in stdio mode → fallback to env)
    allowed_dataset_ids = _get_header(ctx, _HDR_ALLOWED_DATASET_IDS)
    current_user_id = _get_header(ctx, _HDR_CURRENT_USER_ID)

    try:
        from core.kb.external_provider import is_enabled
        from mcp_servers.retrieve_dataset_content_mcp.local_impl import retrieve_public_local_kb

        if not await asyncio.to_thread(is_enabled):
            call = functools.partial(
                retrieve_public_local_kb,
                query=query,
                dataset_id=dataset_id,
                top_k=top_k,
                allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
                current_user_id=current_user_id,
                reranker_enabled=_get_header(ctx, _HDR_RERANKER_ENABLED),
            )
            return await _PRIVATE_KB_LANE.run(call, timeout=_PRIVATE_KB_TIMEOUT_SECONDS)
        items = await retrieve_dataset_content_async(
            query=query,
            dataset_id=dataset_id,
            top_k=top_k,
            score_threshold=score_threshold,
            search_method=search_method,
            reranking_enable=reranking_enable,
            weights=weights,
            allowed_dataset_ids=allowed_dataset_ids,
            current_user_id=current_user_id,
        )
    except (DatasetRetrievalTimeoutError, TimeoutError) as exc:
        _LOGGER.warning("retrieve_dataset_content timed out: %s", exc)
        return tool_timeout_payload(
            tool="retrieve_dataset_content",
            timeout=RETRIEVE_TOTAL_TIMEOUT_SECONDS,
            message=str(exc),
        )
    except DatasetRetrievalUnavailableError as exc:
        _LOGGER.warning("retrieve_dataset_content upstream unavailable: %s", exc)
        return {
            "items": [],
            "error": {
                "code": "upstream_unavailable",
                "tool": "retrieve_dataset_content",
                "message": str(exc),
                "retryable": True,
            },
        }
    except Exception as exc:
        _LOGGER.error("retrieve_dataset_content failed: %s", exc, exc_info=True)
        return {
            "items": [],
            "error": {
                "code": "tool_error",
                "tool": "retrieve_dataset_content",
                "message": "公有知识库检索失败",
                "retryable": True,
            },
        }

    return {"items": items}


# ── List datasets tool ────────────────────────────────────────────────────────


@mcp.tool(description=_LIST_DATASETS_DESCRIPTION)
async def list_datasets(
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """列出全部可用的公有与私有知识库。

    这里只回目录（库名、简介、文档标题）。用户要看/要发**知识库里的图片**时，用
    ``retrieve_local_kb`` 检索该内容——命中结果会带 ``images``（含可直接渲染的 url），
    再按那个工具说明把图输出即可。不要绕去「我的空间」或沙盒找同名文件：那既慢，
    而且发出来的往往是另一份同名文件，不是知识库里的这张。
    """

    from mcp_servers.retrieve_dataset_content_mcp.impl import list_all_datasets as _impl

    allowed_dataset_ids = _get_header(ctx, _HDR_ALLOWED_DATASET_IDS)
    allowed_kb_ids = _get_header(ctx, _HDR_ALLOWED_KB_IDS)
    current_user_id = _get_header(ctx, _HDR_CURRENT_USER_ID)

    call = functools.partial(
        _impl,
        allowed_dataset_ids=allowed_dataset_ids,
        allowed_kb_ids=allowed_kb_ids,
        current_user_id=current_user_id,
    )
    try:
        return await _DATASET_LIST_LANE.run(
            call,
            timeout=_LIST_DATASETS_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        _LOGGER.warning("list_datasets timed out: %s", exc)
        return {
            "public_datasets": [],
            "private_datasets": [],
            "total": 0,
            "error": {
                "code": "tool_timeout",
                "tool": "list_datasets",
                "message": str(exc),
                "retryable": True,
            },
        }
    except Exception as exc:
        _LOGGER.error("list_datasets failed: %s", exc, exc_info=True)
        return {
            "public_datasets": [],
            "private_datasets": [],
            "total": 0,
            "error": {
                "code": "tool_error",
                "tool": "list_datasets",
                "message": "知识库列表加载失败",
                "retryable": True,
            },
        }


# ── Local KB tool ───────────────────────────────────────────────────────────


def _build_local_kb_tool_description() -> str:
    from mcp_servers.retrieve_dataset_content_mcp.impl import _build_runtime_local_kb_section

    runtime_section = _build_runtime_local_kb_section()
    if not runtime_section:
        return _BASE_LOCAL_KB_TOOL_DESCRIPTION
    return f"{_BASE_LOCAL_KB_TOOL_DESCRIPTION}\n\n{runtime_section}".strip()


@mcp.tool(description=_build_local_kb_tool_description())
async def retrieve_local_kb(
    kb_id: str,
    query: str,
    top_k: int = 10,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Retrieve accessible uploaded KBs regardless of their visibility."""

    from mcp_servers.retrieve_dataset_content_mcp.impl import retrieve_local_kb as _impl

    # Read runtime params from HTTP headers (None in stdio mode → fallback to env)
    allowed_kb_ids = _get_header(ctx, _HDR_ALLOWED_KB_IDS)
    current_user_id = _get_header(ctx, _HDR_CURRENT_USER_ID)
    reranker_enabled = _get_header(ctx, _HDR_RERANKER_ENABLED)

    try:
        call = functools.partial(
            _impl,
            kb_id=kb_id,
            query=query,
            top_k=top_k,
            allowed_kb_ids=allowed_kb_ids,
            current_user_id=current_user_id,
            reranker_enabled=reranker_enabled,
        )
        result = await _PRIVATE_KB_LANE.run(
            call,
            timeout=_PRIVATE_KB_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        _LOGGER.warning("retrieve_local_kb timed out: %s", exc)
        return {
            "available_kbs": [],
            **tool_timeout_payload(
                tool="retrieve_local_kb",
                timeout=_PRIVATE_KB_TIMEOUT_SECONDS,
                message=str(exc),
            ),
        }
    except Exception as exc:
        _LOGGER.error("retrieve_local_kb impl failed: %s", exc, exc_info=True)
        result = {
            "available_kbs": [],
            "items": [],
            "error": {
                "code": "tool_error",
                "tool": "retrieve_local_kb",
                "message": "本地知识库检索失败",
                "retryable": True,
            },
        }

    # impl now returns dict with available_kbs + items
    if isinstance(result, dict):
        return result
    # Legacy: list of items
    return {"items": result}


# Wiki tools navigate to original chunks; authorization is checked on every call.
_WIKI_TOOL_NAMES = frozenset(
    {
        "wiki_overview",
        "wiki_locate",
        "wiki_read_page",
        "wiki_expand",
        "wiki_fetch_source",
    }
)

_WIKI_TOOL_TIMEOUT_SECONDS = read_positive_float_env("WIKI_TOOL_TIMEOUT_SECONDS", 30.0)
_WIKI_LANE = BlockingLane(name="wiki", max_workers=3)


async def _run_wiki(tool: str, call, *, empty: Dict[str, Any]) -> Dict[str, Any]:
    """统一跑 wiki 工具：限流 + 超时 + 异常兜底，失败时返回结构化 error。"""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import (
        WikiAccessDeniedError,
        WikiUnsupportedError,
    )

    try:
        return await _WIKI_LANE.run(call, timeout=_WIKI_TOOL_TIMEOUT_SECONDS)
    except WikiAccessDeniedError as exc:
        _LOGGER.warning("%s 越权访问被拒: %s", tool, exc)
        return {
            **empty,
            "error": {
                "code": "access_denied",
                "tool": tool,
                "message": "无权访问该知识库",
                "retryable": False,
            },
        }
    except WikiUnsupportedError as exc:
        _LOGGER.warning("%s 调用到不具备 Wiki 能力的知识库: %s", tool, exc)
        return {
            **empty,
            "error": {
                "code": "unsupported_backend",
                "tool": tool,
                "message": "该知识库不提供 Wiki 能力",
                "retryable": False,
            },
        }
    except TimeoutError as exc:
        _LOGGER.warning("%s timed out: %s", tool, exc)
        return {**empty, **tool_timeout_payload(tool=tool, timeout=_WIKI_TOOL_TIMEOUT_SECONDS)}
    except Exception as exc:
        _LOGGER.error("%s failed: %s", tool, exc, exc_info=True)
        return {
            **empty,
            "error": {
                "code": "tool_error",
                "tool": tool,
                "message": "知识库 Wiki 服务调用失败",
                "retryable": True,
            },
        }


@mcp.tool(description=_WIKI_OVERVIEW_DESCRIPTION)
async def wiki_overview(
    dataset_id: str = "",
    limit: int = 20,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Knowledge base structural overview."""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_overview as _impl

    call = functools.partial(
        _impl,
        dataset_id=dataset_id,
        limit=limit,
        allowed_dataset_ids=_get_header(ctx, _HDR_ALLOWED_DATASET_IDS),
        allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
        current_user_id=_get_header(ctx, _HDR_CURRENT_USER_ID),
    )
    return await _run_wiki("wiki_overview", call, empty={"hub_pages": []})


@mcp.tool(description=_WIKI_LOCATE_DESCRIPTION)
async def wiki_locate(
    query: str,
    dataset_id: str = "",
    limit: int = 8,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Locate relevant concept/entity pages on the knowledge map."""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_locate as _impl

    call = functools.partial(
        _impl,
        query,
        dataset_id=dataset_id,
        limit=limit,
        allowed_dataset_ids=_get_header(ctx, _HDR_ALLOWED_DATASET_IDS),
        allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
        current_user_id=_get_header(ctx, _HDR_CURRENT_USER_ID),
    )
    return await _run_wiki("wiki_locate", call, empty={"pages": []})


@mcp.tool(description=_WIKI_READ_PAGE_DESCRIPTION)
async def wiki_read_page(
    slug: str,
    dataset_id: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Read a single wiki page."""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_read_page as _impl

    call = functools.partial(
        _impl,
        slug,
        dataset_id=dataset_id,
        allowed_dataset_ids=_get_header(ctx, _HDR_ALLOWED_DATASET_IDS),
        allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
        current_user_id=_get_header(ctx, _HDR_CURRENT_USER_ID),
    )
    return await _run_wiki("wiki_read_page", call, empty={})


@mcp.tool(description=_WIKI_EXPAND_DESCRIPTION)
async def wiki_expand(
    slug: str,
    dataset_id: str = "",
    depth: int = 1,
    limit: int = 30,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Expand the concept neighbourhood around a wiki page."""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_expand as _impl

    call = functools.partial(
        _impl,
        slug,
        dataset_id=dataset_id,
        depth=depth,
        limit=limit,
        allowed_dataset_ids=_get_header(ctx, _HDR_ALLOWED_DATASET_IDS),
        allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
        current_user_id=_get_header(ctx, _HDR_CURRENT_USER_ID),
    )
    return await _run_wiki("wiki_expand", call, empty={"nodes": [], "edges": []})


@mcp.tool(description=_WIKI_FETCH_SOURCE_DESCRIPTION)
async def wiki_fetch_source(
    slug: str,
    dataset_id: str = "",
    max_chunks: int = 6,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Fetch the original document chunks a wiki page was derived from."""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_fetch_source as _impl

    call = functools.partial(
        _impl,
        slug,
        dataset_id=dataset_id,
        max_chunks=max_chunks,
        allowed_dataset_ids=_get_header(ctx, _HDR_ALLOWED_DATASET_IDS),
        allowed_kb_ids=_get_header(ctx, _HDR_ALLOWED_KB_IDS),
        current_user_id=_get_header(ctx, _HDR_CURRENT_USER_ID),
    )
    return await _run_wiki("wiki_fetch_source", call, empty={"items": []})


# ── 按后端动态暴露工具 ───────────────────────────────────────────────────────
#
# FastMCP 在 __init__ 里就把 self.list_tools 绑进了 lowlevel handler，所以事后
# 改 mcp.list_tools 不生效——必须对 lowlevel server 重新注册一次（装饰器是
# 覆盖写 request_handlers，不是追加）。


_ORIGINAL_LIST_TOOLS = mcp.list_tools


async def _list_tools_filtered():
    """平台没有 Wiki 源时隐藏 Wiki 工具；公有检索按配置选择后端。

    每次 list_tools 都重新判定，所以切换知识库后端、或新建了一个开 Wiki 的自建库
    之后，无需重启 mcp 容器。
    """
    tools = await _ORIGINAL_LIST_TOOLS()
    try:
        from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import wiki_supported

        if wiki_supported():
            return tools
    except Exception as exc:
        _LOGGER.warning("wiki capability probe failed, hiding wiki tools: %s", exc)
    return [tool for tool in tools if tool.name not in _WIKI_TOOL_NAMES]


mcp._mcp_server.list_tools()(_list_tools_filtered)


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9100)


if __name__ == "__main__":
    main()
