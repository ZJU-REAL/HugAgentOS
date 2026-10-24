"""Provider wire formats. Language filtering is deliberately absent."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import SearchRequest

BAIDU_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
LANGSEARCH_SEARCH_URL = "https://api.langsearch.com/v1/web-search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
KEY_NAMES = {
    "baidu": "BAIDU_API_KEY",
    "deepseek": "DEEPSEEK_SEARCH_API_KEY",
    "langsearch": "LANGSEARCH_API_KEY",
    "tavily": "TAVILY_API_KEY",
}
# DeepSeek rejects longer queries with HTTP 400; operators count toward the limit.
DEEPSEEK_QUERY_CHARS = 128
DEEPSEEK_NEWS_DAYS = 30
# rerank_score is 0–3 (>= 1.5 is "high quality"); only clearly irrelevant rows are dropped.
DEEPSEEK_MIN_RERANK_SCORE = 0.5


class QueryTooLong(ValueError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    engine: str
    api_key: str
    country: str = ""
    auto_parameters: bool = False


def build_request(
    config: ProviderConfig, query: str, options: SearchRequest
) -> tuple[str, dict, dict]:
    headers = {"Content-Type": "application/json"}
    if config.engine == "baidu":
        headers["X-Appbuilder-Authorization"] = f"Bearer {config.api_key}"
        return (
            BAIDU_SEARCH_URL,
            headers,
            {
                "messages": [{"content": query, "role": "user"}],
                "search_source": "baidu_search_v2",
                "resource_type_filter": [{"type": "web", "top_k": options.max_results}],
            },
        )
    headers["Authorization"] = f"Bearer {config.api_key}"
    if config.engine == "deepseek":
        if options.topic == "news":
            since = datetime.now(timezone.utc).date() - timedelta(days=DEEPSEEK_NEWS_DAYS)
            query = f"{query} after:{since.isoformat()}"
        if len(query) > DEEPSEEK_QUERY_CHARS:
            raise QueryTooLong(f"DeepSeek queries are limited to {DEEPSEEK_QUERY_CHARS} characters")
        return (
            DEEPSEEK_SEARCH_URL,
            headers,
            {
                "query": query,
                # Over-fetch so low-relevance rows can be dropped; merge applies the final limit.
                "count": options.max_results * 2,
                "need_content": options.include_raw_content,
            },
        )
    if config.engine == "langsearch":
        return (
            LANGSEARCH_SEARCH_URL,
            headers,
            {
                "query": query,
                "freshness": "noLimit",
                "summary": True,
                "count": options.max_results,
            },
        )
    payload = {
        "query": query,
        "max_results": options.max_results,
        "topic": options.topic,
        "search_depth": options.search_depth,
        "include_raw_content": options.include_raw_content,
        "auto_parameters": config.auto_parameters,
    }
    if config.country and options.topic == "general":
        payload["country"] = config.country
    return TAVILY_SEARCH_URL, headers, payload


def normalize_response(engine: str, data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Invalid provider response")
    if engine == "baidu":
        rows = data.get("references")
    elif engine == "langsearch":
        if data.get("code") != 200:
            raise ValueError("Provider returned an API error")
        rows = data.get("data", {}).get("webPages", {}).get("value")
    elif engine == "deepseek":
        rows = data.get("results")
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            rows = [
                {
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "content": row.get("summary") or row.get("snippet") or "",
                    "published_date": row.get("date"),
                    **({"raw_content": row["content"]} if row.get("content") else {}),
                }
                for row in rows
                if not (
                    isinstance(row.get("rerank_score"), (int, float))
                    and row["rerank_score"] < DEEPSEEK_MIN_RERANK_SCORE
                )
            ]
    else:
        rows = data.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Provider response is missing a results array")
    results = []
    for row in rows:
        item = {
            "title": row.get("name" if engine == "langsearch" else "title") or "",
            "url": row.get("url") or "",
            "content": (
                (row.get("summary") or row.get("snippet") or "")
                if engine == "langsearch"
                else (row.get("content") or "")
            ),
            "published_date": row.get(
                "datePublished" if engine == "langsearch" else "published_date"
            ),
        }
        if engine == "baidu":
            item.update(date_raw=row.get("date"), date_kind="unknown")
        if row.get("raw_content") is not None:
            item["raw_content"] = row["raw_content"]
        if any(not isinstance(item[key], str) for key in ("title", "url", "content")):
            raise ValueError("Invalid source fields")
        if item.get("raw_content") is not None and not isinstance(item["raw_content"], str):
            raise ValueError("Invalid raw content")
        if item["published_date"] is not None and not isinstance(item["published_date"], str):
            raise ValueError("Invalid publication date")
        results.append(item)
    return {
        "results": results,
        "upstream_truncated": (
            data.get("truncated") if isinstance(data.get("truncated"), bool) else None
        ),
    }
