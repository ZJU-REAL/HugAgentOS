"""Markdown detection and source normalisation helpers for chat messages."""

from __future__ import annotations

import re
from typing import Any, Dict, List


def looks_markdown(text: str) -> bool:
    """Return True if *text* appears to contain Markdown formatting."""
    if not text:
        return False
    return bool(re.search(r"\n|```|\*\*|^\s*#\s", text, flags=re.M))


# ------------------------------------------------------------------
# Source normalisation / dedup helpers
# ------------------------------------------------------------------


def source_rank(source_type: str) -> int:
    mapping = {
        "database": 0,
        "db": 0,
        "dataset": 1,
        "knowledge_base": 1,
        "internet": 2,
        "search": 2,
    }
    return mapping.get((source_type or "").strip().lower(), 99)


def _source_key(item: Dict[str, Any]) -> str:
    name = str(item.get("name", "")).strip().lower()
    url = str(item.get("url", "")).strip().lower()
    detail = str(item.get("detail", "")).strip().lower()
    if name:
        return f"name:{name}"
    if url:
        return f"url:{url}"
    return f"detail:{detail}"


def normalize_source(item: Any) -> Dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    source_type = str(item.get("source_type", item.get("type", ""))).strip().lower() or "unknown"
    name = str(item.get("name", item.get("source", ""))).strip()
    detail = str(item.get("detail", item.get("snippet", ""))).strip()
    url = str(item.get("url", "")).strip()
    if not name and not detail and not url:
        return None
    return {
        "source_type": source_type,
        "name": name or "unknown",
        "detail": detail,
        "url": url,
    }


def resolve_sources_conflict(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolve duplicate/conflicting sources by fixed priority.

    Priority: Database > Dataset > Internet.
    """
    bucket: Dict[str, Dict[str, Any]] = {}
    for raw in sources:
        src = normalize_source(raw)
        if src is None:
            continue
        key = _source_key(src)
        old = bucket.get(key)
        if old is None:
            bucket[key] = src
            continue
        if source_rank(src["source_type"]) < source_rank(str(old.get("source_type", ""))):
            bucket[key] = src
    out = list(bucket.values())
    out.sort(key=lambda x: (source_rank(str(x.get("source_type", ""))), str(x.get("name", ""))))
    return out
