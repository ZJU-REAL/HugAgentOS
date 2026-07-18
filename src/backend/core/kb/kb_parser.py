"""Document parser and parent-child chunker for private knowledge base.

Supported formats: PDF, DOCX, TXT, MD, XLSX
Output: list of ParentChunk, each containing child chunks for vector indexing.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ChildChunk:
    child_id: str
    content: str
    index: int          # position within parent


@dataclass
class ParentChunk:
    parent_id: str
    content: str        # full parent text, returned to LLM on retrieval
    children: list[ChildChunk]
    char_start: Optional[int] = None
    char_end: Optional[int] = None


# ── Token counting ─────────────────────────────────────────────────────────────

_tiktoken_enc = None


def _get_tiktoken_enc():
    """Return cached tiktoken cl100k_base encoder."""
    global _tiktoken_enc
    if _tiktoken_enc is None:
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            pass
    return _tiktoken_enc


def _count_tokens(text: str) -> int:
    """Approximate token count using tiktoken cl100k_base."""
    enc = _get_tiktoken_enc()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback: rough Chinese/English estimate
    return max(1, len(text) // 2)


# ── MIME → filename suffix mapping ─────────────────────────────────────────────

_MIME_TO_SUFFIX: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/plain": ".txt",
    "text/markdown": ".txt",
    "text/x-markdown": ".txt",
    "text/csv": ".csv",
    "application/json": ".txt",
}


# ── Text extraction ────────────────────────────────────────────────────────────

def extract_text(file_bytes: bytes, mime_type: str) -> list[dict]:
    """Extract structured paragraphs from a document.

    Delegates to core/file_parser.py for PDF, DOCX, DOC, TXT (uses the
    external file-parser API for PDF, pandoc for DOCX, etc.).
    Falls back to built-in xlsx/plain-text extraction for unsupported types.

    Returns a list of paragraph dicts:
        {"text": str, "heading_level": int|None, "element_type": str}
    """
    mt = (mime_type or "").lower()

    # XLSX — keep built-in handler (file_parser doesn't support spreadsheets)
    if mt in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        return _extract_xlsx(file_bytes)

    # Try core/file_parser for all other known types
    suffix = _MIME_TO_SUFFIX.get(mt)
    if suffix:
        try:
            from core.content.file_parser import parse_file
            filename = f"upload{suffix}"
            content = parse_file(file_bytes, filename)
            if content:
                return _markdown_to_paragraphs(content)
        except RuntimeError:
            # file_parser failed (e.g. service down) — fall through to built-in
            pass

    # Fallback: treat as plain text
    return _extract_plain_text(file_bytes)


def _markdown_to_paragraphs(text: str) -> list[dict]:
    """Parse markdown text (from file_parser) into structured paragraph dicts."""
    paragraphs: list[dict] = []
    table_buffer: list[str] = []

    def _flush_table():
        if table_buffer:
            paragraphs.append({
                "text": "\n".join(table_buffer),
                "heading_level": None,
                "element_type": "table",
            })
            table_buffer.clear()

    for line in text.splitlines():
        stripped = line.strip()

        # Empty line — flush table buffer, skip
        if not stripped:
            _flush_table()
            continue

        # Markdown table row
        if stripped.startswith("|") and stripped.endswith("|"):
            table_buffer.append(stripped)
            continue

        # Not a table row — flush any pending table
        _flush_table()

        # Markdown heading
        md_match = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if md_match:
            level = len(md_match.group(1))
            paragraphs.append({
                "text": stripped,
                "heading_level": level,
                "element_type": "heading",
            })
            continue

        # Chinese legal/report heading patterns
        heading_level = _detect_heading_level(stripped)
        if heading_level:
            paragraphs.append({
                "text": stripped,
                "heading_level": heading_level,
                "element_type": "heading",
            })
            continue

        # Regular paragraph
        paragraphs.append({
            "text": stripped,
            "heading_level": None,
            "element_type": "paragraph",
        })

    _flush_table()
    return paragraphs


def _extract_xlsx(file_bytes: bytes) -> list[dict]:
    import io
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl is required: pip install openpyxl")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    paragraphs = []
    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c or "") for c in rows[0]]
        paragraphs.append({
            "text": f"## Sheet: {sheet.title}",
            "heading_level": 2,
            "element_type": "heading",
        })
        md_lines = ["| " + " | ".join(header) + " |"]
        md_lines.append("|" + "|".join("---" for _ in header) + "|")
        for row in rows[1:]:
            if all(c is None for c in row):
                continue
            md_lines.append("| " + " | ".join(str(c or "") for c in row) + " |")
        paragraphs.append({
            "text": "\n".join(md_lines),
            "heading_level": None,
            "element_type": "table",
        })
    return paragraphs


def _extract_plain_text(file_bytes: bytes) -> list[dict]:
    """Fallback: decode as text and split into paragraphs."""
    # Try common encodings
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = file_bytes.decode("utf-8", errors="replace")

    return _markdown_to_paragraphs(text)


def _detect_heading_level(line: str) -> Optional[int]:
    """Detect Chinese legal/report heading patterns."""
    patterns = [
        (r'^第[一二三四五六七八九十百]+章', 1),
        (r'^第[一二三四五六七八九十百]+节', 2),
        (r'^第[一二三四五六七八九十百\d]+条', 3),
        (r'^[一二三四五六七八九十]+[、．.]', 2),
        (r'^\d+[、．.]\s*\S', 3),
    ]
    for pattern, level in patterns:
        if re.match(pattern, line):
            return level
    return None


# ── Chunking ───────────────────────────────────────────────────────────────────

def build_parent_child_chunks(
    paragraphs: list[dict],
    parent_size: int = 1024,
    child_size: int = 128,
    overlap: int = 20,
    chunk_method: str = "semantic",
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
    child_separators: Optional[list[str]] = None,
) -> list[ParentChunk]:
    """Build parent-child chunk pairs from extracted paragraphs.

    Parent chunks: large semantic units (~parent_size tokens), stored in PostgreSQL,
                   returned to LLM as full context.
    Child chunks: small slices (~child_size tokens), vectorised in Milvus for retrieval.

    ``separators`` (parent-chunk separators): custom separator hierarchy that decides
    parent-chunk boundaries; only effective on paths that go through recursive
    splitting (the recursive method itself, and the recursive fallback of semantic
    chunking for oversized parents); when empty, falls back to the default hierarchy.
    ``child_separators`` (child-chunk separators): when a parent is split into
    children, split on these separators (then pack by child_size); when empty,
    children use the default fixed-length sliding-window split. The two are
    independent and do not affect each other.
    """
    seps = normalize_separators(separators)
    csep = normalize_separators(child_separators)
    if chunk_method == "qa":
        parents = _build_qa_chunks(paragraphs)
    elif chunk_method == "laws":
        parents = _build_law_chunks(paragraphs, parent_size, child_size, overlap)
    elif chunk_method == "recursive":
        parents = _build_recursive_chunks(paragraphs, parent_size, child_size, overlap, separators=seps)
    elif chunk_method == "embedding_semantic":
        parents = _build_embedding_semantic_chunks(
            paragraphs, parent_size, child_size, overlap, embed_fn=embed_fn, separators=seps,
        )
    else:
        # "semantic" / "structured" / default
        parents = _build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Child-chunk separators: re-split children uniformly after parents are built
    # (overriding each builder's default fixed-length children), so we don't have to
    # thread the same parameter into every builder — one post-processing step covers
    # all chunking methods.
    if csep:
        _apply_child_separators(parents, child_size, csep)
    return parents


def _apply_child_separators(parents: list[ParentChunk], child_size: int, separators: list[str]) -> None:
    """Re-split each parent's children by child_separators (replaces ``pc.children`` in place).

    Splits the parent body with the recursive separators and packs by child_size,
    aligning child boundaries to the user-specified separators.
    """
    for pc in parents:
        texts = [s.strip() for s in _recursive_split(pc.content, separators, child_size) if s.strip()]
        pc.children = [
            ChildChunk(child_id=f"{pc.parent_id}_{i}", content=c, index=i)
            for i, c in enumerate(texts or [pc.content])
        ]


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _split_into_children(parent_text: str, child_size: int, overlap: int) -> list[str]:
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


def _make_parent_chunk(text: str, children_texts: list[str]) -> ParentChunk:
    pid = _new_id()
    children = [
        ChildChunk(child_id=f"{pid}_{i}", content=c, index=i)
        for i, c in enumerate(children_texts)
    ]
    return ParentChunk(parent_id=pid, content=text, children=children)


# In structure-aware chunking, a heading is a "preferred break point" that only takes
# effect once the buffer has accumulated to this fraction of parent_size.
# 0.5 was chosen: subsections keep merging into the parent chunk until about half a
# parent_size, then we break before the next heading — preserving structure awareness
# while letting parent_size truly determine parent-chunk size (avoids heading-dense
# documents being shredded).
_HEADING_SOFT_RATIO = 0.5


def _build_structured_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
) -> list[ParentChunk]:
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
    soft_threshold = parent_size * _HEADING_SOFT_RATIO

    def _flush() -> None:
        nonlocal buffer, buffer_tokens
        parent_text = "\n".join(buffer)
        result.append(_make_parent_chunk(parent_text, _split_into_children(parent_text, child_size, overlap)))
        buffer = []
        buffer_tokens = 0

    for para in paragraphs:
        text = para["text"]
        tokens = _count_tokens(text)

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


def _build_law_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
) -> list[ParentChunk]:
    """Split at article/chapter/section boundaries for legal documents."""
    result = []
    buffer: list[str] = []

    article_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+[条章节]')

    for para in paragraphs:
        text = para["text"]
        is_boundary = bool(article_pattern.match(text)) or para.get("heading_level") in (1, 2)

        if is_boundary and buffer:
            parent_text = "\n".join(buffer)
            result.append(_make_parent_chunk(parent_text, _split_into_children(parent_text, child_size, overlap)))
            buffer = []

        buffer.append(text)

    if buffer:
        parent_text = "\n".join(buffer)
        result.append(_make_parent_chunk(parent_text, _split_into_children(parent_text, child_size, overlap)))

    return result


def _build_qa_chunks(paragraphs: list[dict]) -> list[ParentChunk]:
    """Each Q&A pair becomes one parent chunk with one child."""
    result = []
    for para in paragraphs:
        text = para["text"]
        pid = _new_id()
        child = ChildChunk(child_id=f"{pid}_0", content=text, index=0)
        result.append(ParentChunk(parent_id=pid, content=text, children=[child]))
    return result


# ── Recursive chunking ─────────────────────────────────────────────────────────

_RECURSIVE_SEPARATORS = ["\n\n", "\n", "。", ".", "；", ";", "，", ",", " "]

# Users may write escape sequences (e.g. \n, \t) in custom separators; restore them
# to real characters here in one place
_SEPARATOR_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\r": "\r", "\\\\": "\\"}


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
        for token, real in _SEPARATOR_ESCAPES.items():
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


def _recursive_split(text: str, separators: list[str], max_tokens: int) -> list[str]:
    """Recursively split text using a hierarchy of separators.

    Tries to split on the first separator that produces segments.
    Merges adjacent small segments up to max_tokens.
    Over-sized segments recurse with the next separator level.
    Final fallback: character-based truncation.
    """
    if _count_tokens(text) <= max_tokens:
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
            candidate_tokens = _count_tokens(candidate)

            if candidate_tokens <= max_tokens:
                current = candidate
                current_tokens = candidate_tokens
            else:
                if current:
                    result.append(current)
                # If this single part exceeds max_tokens, recurse deeper
                part_tokens = _count_tokens(part)
                if part_tokens > max_tokens:
                    deeper = _recursive_split(part, separators[i + 1:], max_tokens)
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


def _build_recursive_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
    separators: Optional[list[str]] = None,
) -> list[ParentChunk]:
    """Recursive multi-level separator chunking."""
    # Join all paragraph text
    full_text = "\n".join(p["text"] for p in paragraphs)
    segments = _recursive_split(full_text, separators or _RECURSIVE_SEPARATORS, parent_size)

    result = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        result.append(_make_parent_chunk(seg, _split_into_children(seg, child_size, overlap)))
    return result


# ── Embedding-based semantic chunking ──────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _build_embedding_semantic_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
) -> list[ParentChunk]:
    """Semantic chunking based on embedding similarity between adjacent blocks.

    Algorithm:
    1. Merge paragraphs into minimum ~50-token blocks
    2. Batch-embed all blocks
    3. Compute cosine similarity between adjacent blocks
    4. Set breakpoints where similarity drops below P25 or is a local minimum
    5. Merge blocks between breakpoints into parent chunks
    6. Fallback to recursive splitting for oversized parents
    """
    if embed_fn is None:
        # Fallback to structured chunking when no embed function available
        return _build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 1: Merge paragraphs into minimum-size blocks (~50 tokens)
    MIN_BLOCK_TOKENS = 50
    blocks: list[str] = []
    current_block = ""
    current_tokens = 0

    for para in paragraphs:
        text = para["text"]
        tokens = _count_tokens(text)
        if current_block and current_tokens + tokens > MIN_BLOCK_TOKENS:
            blocks.append(current_block)
            current_block = text
            current_tokens = tokens
        else:
            current_block = (current_block + "\n" + text) if current_block else text
            current_tokens += tokens

    if current_block:
        blocks.append(current_block)

    if len(blocks) <= 1:
        # Single block — just use structured
        return _build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 2: Batch embed
    embeddings = embed_fn(blocks)

    # Step 3: Compute adjacent cosine similarities
    similarities: list[float] = []
    for i in range(len(embeddings) - 1):
        sim = _cosine_similarity(embeddings[i], embeddings[i + 1])
        similarities.append(sim)

    if not similarities:
        return _build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 4: Find breakpoints
    sorted_sims = sorted(similarities)
    p25_idx = max(0, len(sorted_sims) // 4 - 1)
    p25_threshold = sorted_sims[p25_idx]

    breakpoints: set[int] = set()
    for i, sim in enumerate(similarities):
        # Below P25 threshold
        if sim <= p25_threshold:
            breakpoints.add(i)
            continue
        # Local minimum: lower than both neighbors
        is_local_min = True
        if i > 0 and sim >= similarities[i - 1]:
            is_local_min = False
        if i < len(similarities) - 1 and sim >= similarities[i + 1]:
            is_local_min = False
        if is_local_min and sim < p25_threshold * 1.2:
            breakpoints.add(i)

    # Step 5: Merge blocks between breakpoints into parent chunks
    result: list[ParentChunk] = []
    segment_start = 0

    sorted_breaks = sorted(breakpoints)
    # Add an artificial breakpoint at the end
    sorted_breaks.append(len(blocks) - 1)

    for bp in sorted_breaks:
        segment_blocks = blocks[segment_start:bp + 1]
        parent_text = "\n".join(segment_blocks).strip()
        segment_start = bp + 1

        if not parent_text:
            continue

        # Step 6: If parent is too large, use recursive split as fallback
        if _count_tokens(parent_text) > parent_size * 1.5:
            sub_segments = _recursive_split(parent_text, separators or _RECURSIVE_SEPARATORS, parent_size)
            for sub in sub_segments:
                sub = sub.strip()
                if sub:
                    result.append(_make_parent_chunk(sub, _split_into_children(sub, child_size, overlap)))
        else:
            result.append(_make_parent_chunk(parent_text, _split_into_children(parent_text, child_size, overlap)))

    # Handle remaining blocks
    if segment_start < len(blocks):
        remaining = "\n".join(blocks[segment_start:]).strip()
        if remaining:
            result.append(_make_parent_chunk(remaining, _split_into_children(remaining, child_size, overlap)))

    return result


# ── Pipeline entry point ───────────────────────────────────────────────────────

def parse_and_chunk(
    file_bytes: bytes,
    mime_type: str,
    chunk_method: str = "semantic",
    parent_size: int = 1024,
    child_size: int = 128,
    overlap: int = 20,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
    child_separators: Optional[list[str]] = None,
) -> list[ParentChunk]:
    """Full pipeline: extract text then build parent-child chunks."""
    paragraphs = extract_text(file_bytes, mime_type)
    return build_parent_child_chunks(
        paragraphs,
        parent_size=parent_size,
        child_size=child_size,
        overlap=overlap,
        chunk_method=chunk_method,
        embed_fn=embed_fn,
        separators=separators,
        child_separators=child_separators,
    )
