"""Per-request citation extraction from tool results.

Each tool has a different output shape; this module normalizes them
into CitationItem objects with a stable id format: "<tool_name>-<index>".
The index is 1-based and scoped per tool call (not globally sequential),
so multiple concurrent tool calls don't collide.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass
class CitationItem:
    id: str  # e.g. "internet_search-1"
    tool_name: str
    tool_id: Optional[str]
    title: str
    url: str
    snippet: str
    source_type: (
        str  # internet | knowledge_base | database | unknown
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_SOURCE_TYPE_MAP: Dict[str, str] = {
    "internet_search": "internet",
    "retrieve_dataset_content": "knowledge_base",
    "retrieve_local_kb": "knowledge_base",
    "query_database": "database",
}


def extract_citations(
    tool_name: str,
    tool_id: Optional[str],
    result: Any,
) -> List[CitationItem]:
    """Extract CitationItem list from a tool result.

    Returns an empty list on any error (never raises).
    """
    source_type = _SOURCE_TYPE_MAP.get(tool_name, "unknown")

    # Normalise raw result to dict
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {"result": result}
    if isinstance(result, list):
        result = {"items": result}
    if not isinstance(result, dict):
        result = {"result": str(result)}

    try:
        if tool_name == "internet_search":
            return _internet_search(tool_id, source_type, result)
        if tool_name == "retrieve_dataset_content":
            return _dataset_content(tool_id, source_type, result)
        if tool_name == "retrieve_local_kb":
            return _local_kb(tool_id, source_type, result)
        if tool_name == "query_database":
            return _database(tool_id, source_type, result)
    except Exception:
        pass
    return []


def extract_citations_with_offset(
    tool_name: str,
    tool_id: Optional[str],
    result: Any,
    citation_offsets: Dict[str, int],
) -> List[CitationItem]:
    """Extract citations and rewrite their ids to stay unique across repeated
    calls to the same tool within one turn / batch item.

    ``extract_citations`` numbers ids ``<tool_name>-<index>`` starting at 1
    *per call* (see module docstring), so a ReAct loop that invokes the same
    tool more than once would otherwise emit duplicate ids (e.g. two
    ``internet_search-1``). This advances a per-tool counter in
    ``citation_offsets`` (mutated in place) and shifts each id's trailing index
    by the accumulated offset, so downstream id-based lookup / dedup
    (frontend reference chips, trajectory distillation, report export) stays
    correct.

    Callers own the ``citation_offsets`` dict (one per turn / item) and keep
    their existing ``.to_dict()`` handling on the returned items.
    """
    cit_items = extract_citations(tool_name, tool_id, result)
    offset = citation_offsets.get(tool_name, 0)
    if offset > 0:
        for cit in cit_items:
            try:
                old_idx = int(cit.id.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                continue
            cit.id = f"{tool_name}-{old_idx + offset}"
    citation_offsets[tool_name] = offset + len(cit_items)
    return cit_items


# ── per-tool extractors ────────────────────────────────────────────────────


def _internet_search(tool_id: Optional[str], source_type: str, data: dict) -> List[CitationItem]:
    sr = data.get("result") or data
    if isinstance(sr, dict):
        results = sr.get("results", [])
    elif isinstance(sr, list):
        results = sr
    else:
        return []

    out: List[CitationItem] = []
    for i, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("url") or "互联网搜索结果")[:120]
        url = str(item.get("url", ""))
        snippet = str(item.get("content") or item.get("snippet") or "")[:300]
        out.append(
            CitationItem(
                id=f"internet_search-{i}",
                tool_name="internet_search",
                tool_id=tool_id,
                title=title,
                url=url,
                snippet=snippet,
                source_type=source_type,
            )
        )
    return out


def _dataset_content(tool_id: Optional[str], source_type: str, data: dict) -> List[CitationItem]:
    items = data.get("items", [])
    out: List[CitationItem] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        # Support both the normalized format and generic external-provider records.
        doc = item.get("document") or {}
        seg = item.get("segment") or {}
        title = str(item.get("文件名称") or doc.get("name") or doc.get("title") or "知识库文档")[
            :120
        ]
        snippet = str(item.get("文件内容") or seg.get("content") or item.get("content") or "")[
            :3000
        ]
        out.append(
            CitationItem(
                id=f"retrieve_dataset_content-{i}",
                tool_name="retrieve_dataset_content",
                tool_id=tool_id,
                title=title,
                url="",
                snippet=snippet,
                source_type=source_type,
            )
        )
    return out


def _local_kb(tool_id: Optional[str], source_type: str, data: dict) -> List[CitationItem]:
    items = data.get("items", [])
    out: List[CitationItem] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            continue
        title = str(item.get("title") or "私有知识库文档")[:120]
        snippet = str(item.get("content") or "")[:3000]
        out.append(
            CitationItem(
                id=f"retrieve_local_kb-{i}",
                tool_name="retrieve_local_kb",
                tool_id=tool_id,
                title=title,
                url="",
                snippet=snippet,
                source_type=source_type,
            )
        )
    return out


def _database(tool_id: Optional[str], source_type: str, data: dict) -> List[CitationItem]:
    res = data.get("result", data)
    snippet = str(res) if not isinstance(res, str) else res
    return [
        CitationItem(
            id="query_database-1",
            tool_name="query_database",
            tool_id=tool_id,
            title="数据库查询结果",
            url="",
            snippet=snippet[:3000],
            source_type=source_type,
        )
    ]
