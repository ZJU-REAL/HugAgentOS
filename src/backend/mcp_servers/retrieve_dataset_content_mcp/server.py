#!/usr/bin/env python3
"""MCP server exposing tools: retrieve_dataset_content & retrieve_local_kb.

Supports two transports:
- stdio (default): spawned per-request as a subprocess
- streamable-http: long-running HTTP server, runtime params via HTTP headers
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("hugagent-retrieve-dataset-content")
_LOGGER = logging.getLogger(__name__)


def _read_positive_float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def _read_positive_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


_PRIVATE_KB_TIMEOUT_SECONDS = _read_positive_float_env("RETRIEVE_LOCAL_KB_TIMEOUT_SECONDS", 30.0)
_PRIVATE_KB_MAX_WORKERS = _read_positive_int_env("RETRIEVE_LOCAL_KB_MAX_WORKERS", 4)
_LIST_DATASETS_TIMEOUT_SECONDS = _read_positive_float_env(
    "LIST_KNOWLEDGE_BASES_TIMEOUT_SECONDS", 30.0
)


class _BlockingLane:
    """Run synchronous work in a dedicated, bounded executor.

    A timed-out caller does not release its slot until the underlying thread
    really finishes. This prevents repeated timeouts from creating an
    unbounded queue of orphaned blocking work.
    """

    def __init__(self, *, name: str, max_workers: int) -> None:
        self._name = name
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"kb-{name}",
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self._max_workers)
        return self._semaphore

    async def run(self, func, *, timeout: float):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        semaphore = self._get_semaphore()

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{self._name} 等待执行槽超过 {timeout:.0f}s") from exc

        try:
            future = loop.run_in_executor(self._executor, func)
        except Exception:
            semaphore.release()
            raise

        # Keep the slot occupied after caller timeout/cancellation until the
        # synchronous operation exits, so executor pressure always stays bounded.
        future.add_done_callback(lambda _: semaphore.release())
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"{self._name} 执行超过 {timeout:.0f}s")

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{self._name} 执行超过 {timeout:.0f}s") from exc


_PRIVATE_KB_LANE = _BlockingLane(name="private-retrieve", max_workers=_PRIVATE_KB_MAX_WORKERS)
_DATASET_LIST_LANE = _BlockingLane(name="dataset-list", max_workers=2)


def _tool_timeout_payload(
    *, tool: str, timeout: float, message: str | None = None
) -> Dict[str, Any]:
    return {
        "items": [],
        "error": {
            "code": "tool_timeout",
            "tool": tool,
            "message": message or f"检索超过 {timeout:.0f} 秒，已停止等待",
            "retryable": True,
        },
    }


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


_BASE_TOOL_DESCRIPTION = """从"知识库/数据集"检索政策文件、报告、非结构化文本片段。默认自动搜索所有可用数据集。

⚠️ 【必须遵守的引用规则】
回答中引用本工具返回的任何内容时，**必须**在引用句末尾加上 `[ref:retrieve_dataset_content-N]` 标记（N 为 items 列表中的序号，从1开始）。
不带引用标记的回答视为不完整，前端将无法展示引用来源卡片。
示例：根据报告，2024年工业增加值增速为5.2%[ref:retrieve_dataset_content-1]。

适用场景（当用户问题涉及以下内容时，应**主动**调用本工具，无需等待用户显式要求）：
- 政策文件原文、解读、申报条件
- 产业分析报告、行业研究、发展规划
- 企业调研材料、项目申报书
- 工业经济运行分析、统计公报等非结构化文本

调用说明：
- **dataset_id 默认留空即可**，系统会自动搜索所有可用数据集并返回最相关的结果。
- 仅当用户明确指定要从某个特定知识库搜索时，才传入对应的 dataset_id。
- 返回的是记录列表；回答时应从每条记录的 `segment -> content` 提取要点。

Args:
    query: 检索 query。
    dataset_id: 数据集 ID（默认为空，自动搜索所有数据集；仅当用户指定特定知识库时才填写）。
    top_k: 返回片段数量。
    score_threshold: 相似度阈值。
    search_method: 检索方式（默认 hybrid_search）。
    reranking_enable: 是否启用重排。
    weights: 混合检索权重。

Returns:
    dict: {"items": [records...]}

调用决策（何时使用我）:
- **优先级**: 高。涉及政策/报告/规划/解读类原文检索时第一优先级。
- 与结构化指标能力的取舍: 我返回的是文档"原文片段"; 指标类工具或技能返回数仓里
  的"结构化数字"。要数字走已启用的指标类能力, 要文段走我。
- 与 retrieve_local_kb 的取舍: 我覆盖公有/共享知识库; retrieve_local_kb 只查用户
  自己上传的私有库。两者不冲突时可并行调用。
- 与 internet_search 的取舍: 内部能找到就别走外网。internet_search 只在我和
  retrieve_local_kb 都没结果时作为兜底。
"""


def _build_tool_description() -> str:
    return _BASE_TOOL_DESCRIPTION


@mcp.tool(description=_build_tool_description())
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
    except DatasetRetrievalTimeoutError as exc:
        _LOGGER.warning("retrieve_dataset_content timed out: %s", exc)
        return _tool_timeout_payload(
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

_LIST_DATASETS_DESCRIPTION = """列出当前可用的所有知识库（公有 + 私有），包含每个知识库的名称、简介和文档列表。

适用场景：
- 用户询问"有哪些知识库"、"有什么数据集"、"知识库列表"等。
- 用户想了解可以查询哪些资料来源。
- 在不确定应该查哪个知识库时，先调用本工具查看可用列表，再用 retrieve_dataset_content 或 retrieve_local_kb 进行检索。

Returns:
    dict: {"public_datasets": [...], "private_datasets": [...], "total": N}
    - public_datasets：公有/共享知识库（含 Dify 外接数据集与本地公有库）。带 dataset_id 的用
      retrieve_dataset_content 检索；带 kb_id 的（本地公有库）用 retrieve_local_kb 检索。
    - private_datasets：仅当前用户自己的私有库（kb_id），用 retrieve_local_kb 检索。
    用户问"有几个公有知识库 / 公有库列表"时以 public_datasets 为准，不要把本地公有库当私有库。
    每个知识库包含：id/名称/简介/文档数量/文档标题列表/type(public|private)
"""


@mcp.tool(description=_LIST_DATASETS_DESCRIPTION)
async def list_datasets(
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """List all available public and private knowledge bases."""

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


# ── Private KB tool ───────────────────────────────────────────────────────────

_BASE_LOCAL_KB_TOOL_DESCRIPTION = """从用户私有知识库中检索相关内容。

⚠️ 【必须遵守的引用规则】
回答中引用本工具返回的任何内容时，**必须**在引用句末尾加上 `[ref:retrieve_local_kb-N]` 标记（N 为 items 列表中的序号，从1开始）。
不带引用标记的回答视为不完整，前端将无法展示引用来源卡片。
示例：项目总投资额为3.5亿元[ref:retrieve_local_kb-1]。

适用场景（当用户问题涉及以下内容时，应**主动**调用本工具，无需等待用户显式要求）：
- 用户私人上传的文档（项目材料、个人笔记、专属报告等）
- 用户提问中出现了下方"当前可用私有知识库"列表里的知识库名称或文档名称

调用说明：
- 如不确定有哪些私有知识库可用，请先调用 `list_datasets` 工具查看完整知识库列表及其文档目录。
- 如果下方有"当前可用私有知识库"列表，kb_id 应从中选择。
- 如果没有列表或不确定 kb_id，可以传空字符串 ""，系统会自动搜索用户所有私有知识库。
- 返回结果包含 available_kbs（可用知识库列表）和 items（检索结果）。
- 每条 item 含 id, title, content, kb_id, score。

Args:
    kb_id: 私有知识库 ID（可传空字符串以搜索所有私有库）。
    query: 检索问题。
    top_k: 返回片段数量（默认 10）。

Returns:
    dict: {"available_kbs": [{"kb_id": "...", "name": "..."}], "items": [{"title": "...", "content": "...", "kb_id": "...", "score": ...}]}

调用决策（何时使用我）:
- **优先级**: 高。用户问到自己上传的文档/项目材料/个人笔记/专属报告时第一优先级。
- 与 retrieve_dataset_content 的取舍: 我只查用户私有知识库（kb_id 以 kb_ 开头）;
  retrieve_dataset_content 查公有数据集。如果用户没明说"我上传的"还是"政策文件"，
  两者都试一遍。
- kb_id 不确定: 先调 list_datasets 拿可用列表，或直接传 ""（空字符串）让系统搜全量
  私有库。
"""


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
    """Execute private KB retrieval and return MCP-compatible payload."""

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
            **_tool_timeout_payload(
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
                "message": "私有知识库检索失败",
                "retryable": True,
            },
        }

    # impl now returns dict with available_kbs + items
    if isinstance(result, dict):
        return result
    # Legacy: list of items
    return {"items": result}


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9100)


if __name__ == "__main__":
    main()
