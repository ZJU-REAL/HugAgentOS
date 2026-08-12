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
回答中引用本工具返回的任何内容时，**必须**带引用标记：把该条目自带的 `cite_id`（如 `e7`）原样复制进 `[锚文本](cite:e7)` 或句末 `[来源](cite:e7)`，禁止自行编号。
不带引用标记的回答视为不完整，前端将无法展示引用来源卡片。
示例：根据报告，2024年工业增加值增速为5.2%[来源](cite:e7)。

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
    - public_datasets：公有/共享知识库（含外接数据集与本地公有库）。带 dataset_id 的用
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
回答中引用本工具返回的任何内容时，**必须**带引用标记：把该条目自带的 `cite_id`（如 `e7`）原样复制进 `[锚文本](cite:e7)` 或句末 `[来源](cite:e7)`，禁止自行编号。
不带引用标记的回答视为不完整，前端将无法展示引用来源卡片。
示例：项目总投资额为3.5亿元[来源](cite:e7)。

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


# ── LLM Wiki / 概念图谱工具（仅 Wiki-capable 后端暴露） ──────────────────────
#
# 这五个工具服务于同一条单向链：定位 → 展开 → 顺血缘取原文。Wiki 是知识库的
# 地图，不是第二个检索源——它负责把范围收窄，答案和出处仍来自原文分块。
#
# dify / fastgpt / 社区版下没有这层产物，工具会被 list_tools 整体过滤掉，模型
# 看不到它们的存在（见文件末尾的 _list_tools_filtered）。

_WIKI_TOOL_NAMES = frozenset(
    {
        "wiki_overview",
        "wiki_locate",
        "wiki_read_page",
        "wiki_expand",
        "wiki_fetch_source",
    }
)

_WIKI_TOOL_TIMEOUT_SECONDS = _read_positive_float_env("WIKI_TOOL_TIMEOUT_SECONDS", 30.0)
_WIKI_LANE = _BlockingLane(name="wiki", max_workers=3)


async def _run_wiki(tool: str, call, *, empty: Dict[str, Any]) -> Dict[str, Any]:
    """统一跑 wiki 工具：限流 + 超时 + 异常兜底，失败时返回结构化 error。"""
    from mcp_servers.retrieve_dataset_content_mcp.wiki_impl import WikiUnsupportedError

    try:
        return await _WIKI_LANE.run(call, timeout=_WIKI_TOOL_TIMEOUT_SECONDS)
    except WikiUnsupportedError as exc:
        _LOGGER.warning("%s called on a non-wiki backend: %s", tool, exc)
        return {
            **empty,
            "error": {
                "code": "unsupported_backend",
                "tool": tool,
                "message": "当前知识库后端不提供 Wiki 能力",
                "retryable": False,
            },
        }
    except TimeoutError as exc:
        _LOGGER.warning("%s timed out: %s", tool, exc)
        return {**empty, **_tool_timeout_payload(tool=tool, timeout=_WIKI_TOOL_TIMEOUT_SECONDS)}
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


_WIKI_OVERVIEW_DESCRIPTION = """查看某个知识库的**结构地图总览**：一共有哪些概念、规模多大、哪些是主干概念。

适用场景：
- 用户问"这个知识库里有什么"、"都涵盖哪些方面"、"整体讲了什么"。
- 你不确定该从哪里查起时，先看总览找主干，再用 wiki_locate 精确定位。

Args:
    dataset_id: 知识库 ID（留空自动选用当前可用的知识库）。
    limit: 返回多少个枢纽概念（默认 20）。

Returns:
    dict: {"total_pages", "pages_by_type", "total_links", "hub_pages": [...]}

调用决策（何时使用我）:
- **优先级**: 中。属于"探路"工具，不直接产出答案。
- 与 list_datasets 的取舍: list_datasets 回答"有哪些知识库"; 我回答"某个知识库
  内部的知识结构长什么样"。
"""


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
    )
    return await _run_wiki("wiki_overview", call, empty={"hub_pages": []})


_WIKI_LOCATE_DESCRIPTION = """【第①步·定位】在知识库的**概念地图**上定位问题落在哪些概念/实体上。

知识库为每篇文档抽出了概念页和实体页，并把它们互相链接成一张图。本工具按关键词
命中这些页面，返回标题、摘要和关系数量——**不返回长正文**，它只负责告诉你"该看哪里"。

典型三步用法：
1. `wiki_locate` 定位到相关概念页；
2. 需要看全貌时 `wiki_expand` 沿关系展开；
3. **必须**用 `wiki_fetch_source` 顺血缘取回原文，再据原文作答。

⚠️ 不要直接拿本工具返回的 summary 当答案——那是模型二次加工过的概述，可能失真。
答案与出处一律以 wiki_fetch_source 取回的原文为准。

命中为空时的补救（按顺序试）：
- 换更书面的术语（口语说法常常匹配不上，如"牌照"对不上《运营资质证书》）；
- 用正则交替一次给多个说法：`资质|牌照|证书`；
- 仍为空则改用 retrieve_dataset_content 走原文语义检索。

Args:
    query: 检索词，支持正则交替（如 `编制|员额`）。
    dataset_id: 知识库 ID（留空自动选用当前可用的知识库）。
    limit: 返回条数（默认 8）。

Returns:
    dict: {"pages": [{"slug","title","type","summary","related_count","source_doc_count"}]}

调用决策（何时使用我）:
- **优先级**: 高。问题涉及"某个概念/机构/制度是什么、和什么有关"时先走我。
- 与 retrieve_dataset_content 的取舍: 我做**定位**（快、准、给结构）; 它做**语义
  召回**（口语化提问更稳）。术语明确走我，口语化或我命中为空走它。
"""


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
    )
    return await _run_wiki("wiki_locate", call, empty={"pages": []})


_WIKI_READ_PAGE_DESCRIPTION = """读取某个 Wiki 概念页的完整内容与关系。

先用 wiki_locate 拿到 slug，再用本工具精读。返回的正文里 `[[slug|显示名]]` 是指向
其他概念页的链接，可以继续读。

⚠️ 页面正文是模型综合原文写成的**概述**，作答的事实依据应来自 wiki_fetch_source
取回的原文分块。

Args:
    slug: 页面标识，形如 `entity/example-city` 或 `concept/xin-yong`。
    dataset_id: 知识库 ID（留空自动选用当前可用的知识库）。

Returns:
    dict: {"title","type","content","related_pages","referenced_by","has_source"}

调用决策（何时使用我）:
- **优先级**: 中。wiki_locate 之后的精读步骤。
- 只想要事实和出处、不需要概览时，可以跳过我直接 wiki_fetch_source。
"""


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
    )
    return await _run_wiki("wiki_read_page", call, empty={})


_WIKI_EXPAND_DESCRIPTION = """【第②步·展开】沿概念之间的关系，把与某个概念相关的其他概念一次拉齐。

**聚合型问题的关键一步。**"一共有几类"、"彼此什么依赖"这种问题，靠相似度取前 N
个片段天然答不全——必须沿着概念图把该看的都找齐，再逐个回原文核实。

Args:
    slug: 中心概念的 slug（来自 wiki_locate）。
    dataset_id: 知识库 ID（留空自动选用当前可用的知识库）。
    depth: 展开层数，1=直接相关，2=再往外一层（默认 1，最大 3）。
    limit: 最多返回多少个相关概念（默认 30）。

Returns:
    dict: {"nodes": [{"slug","title","type","link_count"}], "edges": [...], "truncated": bool}

调用决策（何时使用我）:
- **优先级**: 中高。问题里出现"一共""哪些""分别""彼此关系""全部要求"时用我。
- 单点事实问题（某个数值、某个期限）不需要我，locate 完直接 fetch_source。
- truncated=true 说明邻域被截断了，作答时应说明这一点，别声称已穷尽。
"""


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
    )
    return await _run_wiki("wiki_expand", call, empty={"nodes": [], "edges": []})


_WIKI_FETCH_SOURCE_DESCRIPTION = """【第③步·取原文】顺着 Wiki 页面记录的血缘坐标，取回它所依据的**原始文档段落**。

这是按 ID 直接取回，不是再检索一次——所以既快又不会取错段落。

⚠️ 【必须遵守的引用规则】
回答中引用本工具返回的任何内容时，**必须**带引用标记：把该条目自带的 `cite_id`
（如 `e7`）原样复制进 `[锚文本](cite:e7)` 或句末 `[来源](cite:e7)`，禁止自行编号。
不带引用标记的回答视为不完整，前端将无法展示引用来源卡片。
示例：《运营资质证书》有效期为五年[来源](cite:e7)。

**作答的事实依据必须来自本工具返回的原文**，不要用 Wiki 页面的概述代替原文。

Args:
    slug: 页面标识（来自 wiki_locate / wiki_expand）。
    dataset_id: 知识库 ID（留空自动选用当前可用的知识库）。
    max_chunks: 最多取回几段原文（默认 6）。

Returns:
    dict: {"wiki_page": "...", "items": [{"文件名称","文件内容","document_id","chunk_id"}]}

调用决策（何时使用我）:
- **优先级**: 高。走了 wiki_locate / wiki_expand 之后**必须**走我再作答。
- 与 retrieve_dataset_content 的取舍: 我是"按坐标直取已定位的原文"; 它是"重新做一次
  语义检索"。已经定位到概念页就用我，没定位到才用它。
"""


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
    )
    return await _run_wiki("wiki_fetch_source", call, empty={"items": []})


# ── 按后端动态暴露工具 ───────────────────────────────────────────────────────
#
# FastMCP 在 __init__ 里就把 self.list_tools 绑进了 lowlevel handler，所以事后
# 改 mcp.list_tools 不生效——必须对 lowlevel server 重新注册一次（装饰器是
# 覆盖写 request_handlers，不是追加）。

_ORIGINAL_LIST_TOOLS = mcp.list_tools


async def _list_tools_filtered():
    """Wiki 工具只在支持的后端上出现在工具清单里。

    每次 list_tools 都重新判定，所以在管理台切换知识库后端后无需重启 mcp 容器。
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
