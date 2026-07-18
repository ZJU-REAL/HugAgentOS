"""Message content extraction and parsing helpers.

Utilities for extracting text from message objects,
stream items, and mixed-content message chunks.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


def format_message_content(content: Any) -> str:
    """Format heterogeneous message content into a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item.get("text", "")))
                elif "content" in item:
                    parts.append(str(item.get("content", "")))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join([x for x in parts if x])
    return str(content)


def looks_markdown(text: str) -> bool:
    """Return True if *text* appears to contain Markdown formatting."""
    if not text:
        return False
    return bool(re.search(r"\n|```|\*\*|^\s*#\s", text, flags=re.M))


def extract_text_from_stream_item(item: Any) -> str:
    """Extract textual delta from a message stream item."""
    if item is None:
        return ""

    content = getattr(item, "content", item)
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = str(block.get("type", "")).strip().lower()
                if block_type in {"text", "output_text"}:
                    text = str(block.get("text", ""))
                    if text:
                        parts.append(text)
                elif "content" in block:
                    text = str(block.get("content", ""))
                    if text:
                        parts.append(text)
            elif isinstance(block, str) and block:
                parts.append(block)
        if parts:
            return "".join(parts)

    content_blocks = getattr(item, "content_blocks", None)
    if isinstance(content_blocks, list):
        parts2: List[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).strip().lower()
            if block_type not in {"text", "output_text"}:
                continue
            text = str(block.get("text", ""))
            if text:
                parts2.append(text)
        if parts2:
            return "".join(parts2)

    return ""


def extract_text_from_messages_chunk(chunk: Any) -> Tuple[str, str, Any]:
    """Extract delta, node type, and full message from a messages chunk.

    This function handles tuple-of-(message, metadata) format used by
    the streaming infrastructure.

    Returns:
        Tuple of (text_delta, node_type, message_like).
        node_type is one of: "model", "tools", "unknown"
    """
    if not (isinstance(chunk, tuple) and len(chunk) == 2):
        return "", "unknown", None
    message_like, metadata = chunk
    node_type = "unknown"
    if isinstance(metadata, dict):
        node_name = str(metadata.get("node_type", metadata.get("langgraph_node", ""))).strip().lower()
        if node_name == "tools":
            node_type = "tools"
        elif node_name == "model":
            node_type = "model"
    text = extract_text_from_stream_item(message_like)
    return text, node_type, message_like


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
