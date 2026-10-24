"""parser chunks responsibilities."""
from __future__ import annotations
import json
import re
import uuid
from typing import Optional
from features.knowledge import kb_parser_state
from features.knowledge import parser_tokens
from features.knowledge import parser_types

def apply_child_separators(parents: list[parser_types.ParentChunk], child_size: int, separators: list[str]) -> None:
    """Re-split each parent's children by child_separators (replaces ``pc.children`` in place).

    Splits the parent body with the recursive separators and packs by child_size,
    aligning child boundaries to the user-specified separators.
    """
    for pc in parents:
        texts = [s.strip() for s in recursive_split(pc.content, separators, child_size) if s.strip()]
        pc.children = [
            parser_types.ChildChunk(child_id=f"{pc.parent_id}_{i}", content=c, index=i)
            for i, c in enumerate(texts or [pc.content])
        ]


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def split_into_children(parent_text: str, child_size: int, overlap: int) -> list[str]:
    """Split parent text into overlapping child chunks by token count."""
    words = parent_text.split()
    if not words:
        return [parent_text]

    # Use character-based approximation when tiktoken unavailable
    chars_per_token = 2  # conservative for Chinese
    child_chars = child_size * chars_per_token
    overlap_chars = overlap * chars_per_token

    children = []
    start = 0
    while start < len(parent_text):
        end = start + child_chars
        chunk = parent_text[start:end].strip()
        if chunk:
            children.append(chunk)
        if end >= len(parent_text):
            break
        start = end - overlap_chars
    return children if children else [parent_text]


def make_parent_chunk(text: str, children_texts: list[str]) -> parser_types.ParentChunk:
    pid = new_id()
    children = [
        parser_types.ChildChunk(child_id=f"{pid}_{i}", content=c, index=i)
        for i, c in enumerate(children_texts)
    ]
    return parser_types.ParentChunk(parent_id=pid, content=text, children=children)


def build_structured_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
) -> list[parser_types.ParentChunk]:
    """Structure-aware chunking.

    A heading is a "preferred break point", not a "forced break point": we only break
    before the next heading once the buffer has accumulated to a certain fraction of
    parent_size (``_HEADING_SOFT_RATIO``); otherwise consecutive subsections are
    merged into the same parent chunk until it approaches parent_size. This keeps
    the heading/paragraph-aligned structure awareness while making ``parent_size``
    actually effective — heading-dense documents are no longer cut into lots of tiny
    pieces, and parents can yield enough children.
    """
    result = []
    buffer: list[str] = []
    buffer_tokens = 0
    soft_threshold = parent_size * kb_parser_state.HEADING_SOFT_RATIO

    def _flush() -> None:
        nonlocal buffer, buffer_tokens
        parent_text = "\n".join(buffer)
        result.append(make_parent_chunk(parent_text, split_into_children(parent_text, child_size, overlap)))
        buffer = []
        buffer_tokens = 0

    for para in paragraphs:
        text = para["text"]
        tokens = parser_tokens.count_tokens(text)

        # Hard cap: adding this paragraph would exceed parent_size, so break off the
        # accumulated content first
        if buffer and buffer_tokens + tokens > parent_size:
            _flush()
        # Soft boundary: break before a heading only when enough content has
        # accumulated (>= a fraction of parent_size); otherwise merge this
        # subsection into the current parent so parent_size is the real determinant
        # of parent-chunk size
        elif buffer and para.get("heading_level") and buffer_tokens >= soft_threshold:
            _flush()

        buffer.append(text)
        buffer_tokens += tokens

    if buffer:
        _flush()

    return result


def build_law_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
) -> list[parser_types.ParentChunk]:
    """Split at article/chapter/section boundaries for legal documents."""
    result = []
    buffer: list[str] = []

    article_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+[条章节]')

    for para in paragraphs:
        text = para["text"]
        is_boundary = bool(article_pattern.match(text)) or para.get("heading_level") in (1, 2)

        if is_boundary and buffer:
            parent_text = "\n".join(buffer)
            result.append(make_parent_chunk(parent_text, split_into_children(parent_text, child_size, overlap)))
            buffer = []

        buffer.append(text)

    if buffer:
        parent_text = "\n".join(buffer)
        result.append(make_parent_chunk(parent_text, split_into_children(parent_text, child_size, overlap)))

    return result


def build_qa_chunks(paragraphs: list[dict]) -> list[parser_types.ParentChunk]:
    """Each Q&A pair becomes one parent chunk with one child."""
    result = []
    for para in paragraphs:
        text = para["text"]
        pid = new_id()
        child = parser_types.ChildChunk(child_id=f"{pid}_0", content=text, index=0)
        result.append(parser_types.ParentChunk(parent_id=pid, content=text, children=[child]))
    return result


def normalize_separators(seps: Optional[list[str]]) -> Optional[list[str]]:
    """Normalize user-supplied custom separators into a usable hierarchy list.

    - Restore escapes like ``\\n`` / ``\\t``;
    - Drop empty strings, de-duplicate while preserving order;
    - Return None when all empty (callers fall back to the ``_RECURSIVE_SEPARATORS`` default hierarchy).
    """
    if not seps:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in seps:
        if raw is None:
            continue
        s = str(raw)
        for token, real in kb_parser_state.SEPARATOR_ESCAPES.items():
            s = s.replace(token, real)
        if s == "" or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out or None


def parse_separators_form(raw: Optional[str]) -> Optional[list[str]]:
    """Parse custom separators passed as a JSON-array string in a multipart form into a list.

    The preview endpoint takes Form params and cannot receive a list directly, so by
    convention a JSON string is passed (e.g. ``["\\n\\n", "。"]``). On parse failure
    or non-list input, silently return None (fall back to the default hierarchy)
    instead of 500-ing over tuning input.
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return normalize_separators([str(x) for x in data])


def recursive_split(text: str, separators: list[str], max_tokens: int) -> list[str]:
    """Recursively split text using a hierarchy of separators.

    Tries to split on the first separator that produces segments.
    Merges adjacent small segments up to max_tokens.
    Over-sized segments recurse with the next separator level.
    Final fallback: character-based truncation.
    """
    if parser_tokens.count_tokens(text) <= max_tokens:
        return [text]

    for i, sep in enumerate(separators):
        parts = text.split(sep)
        if len(parts) <= 1:
            continue

        # Merge adjacent small parts up to max_tokens
        result: list[str] = []
        current = ""
        current_tokens = 0

        for part in parts:
            candidate = (current + sep + part) if current else part
            candidate_tokens = parser_tokens.count_tokens(candidate)

            if candidate_tokens <= max_tokens:
                current = candidate
                current_tokens = candidate_tokens
            else:
                if current:
                    result.append(current)
                # If this single part exceeds max_tokens, recurse deeper
                part_tokens = parser_tokens.count_tokens(part)
                if part_tokens > max_tokens:
                    deeper = recursive_split(part, separators[i + 1:], max_tokens)
                    result.extend(deeper)
                    current = ""
                    current_tokens = 0
                else:
                    current = part
                    current_tokens = part_tokens

        if current:
            result.append(current)

        if result:
            return result

    # Final fallback: character-based truncation
    chars_per_token = 2
    chunk_chars = max_tokens * chars_per_token
    result = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        chunk = text[start:end].strip()
        if chunk:
            result.append(chunk)
        start = end
    return result if result else [text]


def build_recursive_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
    separators: Optional[list[str]] = None,
) -> list[parser_types.ParentChunk]:
    """Recursive multi-level separator chunking."""
    # Join all paragraph text
    full_text = "\n".join(p["text"] for p in paragraphs)
    segments = recursive_split(full_text, separators or kb_parser_state.RECURSIVE_SEPARATORS, parent_size)

    result = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        result.append(make_parent_chunk(seg, split_into_children(seg, child_size, overlap)))
    return result
