"""Deterministic source selection and explicit content budgets."""

from datetime import datetime, timezone
from itertools import zip_longest
from urllib.parse import urlsplit, urlunsplit

SUMMARY_CHARS = 2000
RAW_CHARS = 8000
TOTAL_CONTENT_CHARS = 24000


def url_key(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return None


def merge_sources(batches: list[dict], limit: int) -> dict:
    selected: dict[str, dict] = {}
    seen: set[str] = set()
    invalid = 0
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for row in zip_longest(*(batch["results"] for batch in batches)):
        for index, item in enumerate(row):
            if item is None:
                continue
            key = url_key(str(item.get("url") or ""))
            if key is None:
                invalid += 1
                continue
            seen.add(key)
            if key in selected:
                existing = selected[key]
                if index not in existing["matched_queries"]:
                    existing["matched_queries"].append(index)
                for field in (
                    "title",
                    "content",
                    "raw_content",
                    "published_date",
                    "date_raw",
                    "date_kind",
                ):
                    if not existing.get(field) and item.get(field):
                        existing[field] = item[field]
                if (
                    existing.get("published_date")
                    and item.get("published_date")
                    and existing["published_date"] != item["published_date"]
                ):
                    existing["date_conflict"] = True
                continue
            if len(selected) < limit:
                selected[key] = {
                    **item,
                    "matched_queries": [index],
                    "retrieved_at": retrieved_at,
                    "content_truncated": False,
                }
    sources = list(selected.values())
    per_source = TOTAL_CONTENT_CHARS // max(1, len(sources))
    for source in sources:
        remaining = per_source
        for field, maximum in (("content", SUMMARY_CHARS), ("raw_content", RAW_CHARS)):
            if field not in source:
                continue
            value = source[field] or ""
            kept = value[: min(maximum, remaining)]
            source[field] = kept
            source["content_truncated"] |= len(kept) < len(value)
            remaining -= len(kept)
        source["matched_queries"].sort()
    upstream = [batch.get("upstream_truncated") for batch in batches]
    return {
        "results": sources,
        "results_limited": len(seen) > len(sources),
        "upstream_truncated": (
            True if True in upstream else (False if all(v is False for v in upstream) else None)
        ),
        "invalid_source_count": invalid,
    }
