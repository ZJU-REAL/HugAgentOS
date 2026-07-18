"""Implementation for MCP tool: internet_search.

This module is imported by the MCP stdio server only.
Keep it focused: one tool per folder.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from tavily import TavilyClient

# Import safe stream writer from common utilities
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from _common import safe_stream_writer

from core.config.runtime_env import get_runtime_value

load_dotenv()

_tavily_client: TavilyClient | None = None
_tavily_client_key: str | None = None  # detect admin-panel rotations
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

BAIDU_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_httpx_client: httpx.Client | None = None


def _get_tavily_client() -> TavilyClient:
    """Build the Tavily client lazily and rebuild on admin-panel key rotation."""
    global _tavily_client, _tavily_client_key
    key = (get_runtime_value("TAVILY_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY is missing")
    if _tavily_client is None or key != _tavily_client_key:
        _tavily_client = TavilyClient(api_key=key)
        _tavily_client_key = key
    return _tavily_client


def _get_httpx_client() -> httpx.Client:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.Client(timeout=30.0)
    return _httpx_client


def _baidu_search(query: str, max_results: int = 5) -> dict:
    """Call Baidu AI Search API and return results in Tavily-compatible format."""
    api_key = (get_runtime_value("BAIDU_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("BAIDU_API_KEY is missing")

    resp = _get_httpx_client().post(
        BAIDU_SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Appbuilder-Authorization": f"Bearer {api_key}",
        },
        json={
            "messages": [{"content": query, "role": "user"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": max_results}],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return _normalize_baidu_response(data, max_results)


def _normalize_baidu_response(data: dict, max_results: int) -> dict:
    """Convert Baidu AI Search response to Tavily-compatible result dict.

    Baidu response uses "references" as the top-level key, each item has:
    id, title, url, content, date, type, icon, image, video, web_anchor.
    """
    raw_results = data.get("references") or []
    results = []
    for item in raw_results[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", ""),
        })
    return {"results": results}


def _env_bool(name: str, default: bool) -> bool:
    raw = (get_runtime_value(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = (get_runtime_value(name) or "").strip()
    return raw or default


def _is_cn_result(item: dict) -> bool:
    url = str(item.get("url") or "")
    host = urlparse(url).netloc.lower()
    is_cn_host = host.endswith(".cn") or host.endswith(".中国")

    title = str(item.get("title") or "")
    content = str(item.get("content") or "")
    raw_content = str(item.get("raw_content") or "")
    has_cjk = bool(_HAS_CJK_RE.search(f"{title}\n{content}\n{raw_content}"))

    return is_cn_host or has_cjk


def _filter_cn_results(search_result: dict, max_results: int) -> dict:
    results = search_result.get("results")
    if not isinstance(results, list):
        return search_result

    filtered = [r for r in results if isinstance(r, dict) and _is_cn_result(r)]
    out = dict(search_result)
    out["results"] = filtered[:max_results]
    return out


def internet_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "advanced",
    include_raw_content: bool = False,
    cn_only: bool | None = None,
):
    writer = safe_stream_writer()
    engine = _env_str("INTERNET_SEARCH_ENGINE", "tavily").lower()
    writer(f"正在通过{'百度' if engine == 'baidu' else 'Tavily'}搜索{query}的结果...\n")

    resolved_cn_only = _env_bool("INTERNET_SEARCH_CN_ONLY", True) if cn_only is None else cn_only

    if engine == "baidu":
        search_result = _baidu_search(query, max_results=max_results)
    else:
        search_result = _get_tavily_client().search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
            search_depth=search_depth,
            country=_env_str("INTERNET_SEARCH_COUNTRY", "china"),
            auto_parameters=_env_bool("INTERNET_SEARCH_AUTO_PARAMETERS", False),
        )

    if not resolved_cn_only or not isinstance(search_result, dict):
        return search_result

    filtered = _filter_cn_results(search_result, max_results=max_results)
    results = filtered.get("results")
    if isinstance(results, list) and results:
        return filtered

    if _env_bool("INTERNET_SEARCH_CN_STRICT", True):
        out = dict(search_result)
        out["results"] = []
        out["warning"] = "未命中中文结果（已启用严格中文过滤）"
        return out

    return search_result
