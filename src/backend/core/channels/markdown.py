"""Outbound channel Markdown adaptation: citation-marker cleanup + per-channel capability downgrade.

LLM replies are web-UI-flavored Markdown (with ``[ref:tool-N]`` citation
markers, tables, code fences, etc.); each IM channel only accepts a subset:

- DingTalk markdown messages officially support headings / bold / italic /
  links / images / ordered & unordered lists / blockquotes; **tables and code
  fences are outside the official guarantee** (some PC versions render them,
  mobile mostly doesn't) → tables are downgraded to "field: value" line lists,
  fence lines are removed (code content kept verbatim).
- ``[ref:tool-N]`` is only rendered as a citation badge on the web UI; in IM it
  is garbled noise → always stripped before sending (applies to all channels,
  regardless of whether markdown is used).

This module only does stateless text transforms, reused by
adapter / inbound / outbound. Downgrade functions are idempotent: running
already-downgraded text through again is a no-op, so callers need not worry
about double processing.
"""

from __future__ import annotations

import re
from typing import List

# Citation markers: [ref:internet_search-1] etc. (id shape: see orchestration/citations.py)
_REF_RE = re.compile(r"\[ref:[^\[\]]{1,64}\]")

# Code fence lines (start with ``` / ~~~, may carry a language tag)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Cells of a GFM table separator row: --- / :--- / ---: / :---:
_TABLE_SEP_CELL_RE = re.compile(r":?-+:?")


def strip_citation_markers(text: str) -> str:
    """Strip [ref:xxx-N] citation markers from LLM output (IM channels cannot render them; pure noise)."""
    text = text or ""
    if "[ref:" not in text:
        return text
    return _REF_RE.sub("", text)


def derive_title(text: str, fallback: str = "新消息", limit: int = 20) -> str:
    """Distill a one-line plain-text title from Markdown body (DingTalk markdown
    messages require title; shown in conversation-list summaries/notifications).
    Take the first non-empty line, strip block/inline markup, then truncate."""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)        # images → drop
        s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)    # links → keep text only
        s = re.sub(r"^[#>\s]+", "", s)                     # heading/quote prefix
        s = re.sub(r"^([-*+]|\d+\.)\s+", "", s)            # list prefix
        s = s.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
        s = s.strip("* ").strip()
        if s:
            return s[:limit]
    return fallback


def downgrade_for_dingtalk(text: str) -> str:
    """Downgrade syntax DingTalk can't render into readable plain text: tables → line lists; fence lines removed.

    Only converts when a **well-formed table** (header + separator row) is
    recognized; everything else is kept as-is — better to leave it alone than
    make lossy guesses. Idempotent: the converted output contains no more
    ``|`` tables or fence lines.
    """
    lines = (text or "").splitlines()
    out: List[str] = []
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            i += 1  # drop the fence line itself; keep fenced content verbatim
            continue
        if not in_fence and _is_table_line(line):
            j = i
            while j < len(lines) and _is_table_line(lines[j]):
                j += 1
            out.extend(_convert_table(lines[i:j]))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(_TABLE_SEP_CELL_RE.fullmatch(c) for c in cells if c) and any(cells)


def _convert_table(block: List[str]) -> List[str]:
    """Well-formed table (first row header, second row separator) → one "- header: value｜…" per data row; otherwise return as-is."""
    rows = [_split_row(l) for l in block]
    if len(rows) < 2 or not _is_separator_row(rows[1]):
        return block
    headers = rows[0]
    out: List[str] = []
    for cells in rows[2:]:
        pairs = [f"{h}: {c}" for h, c in zip(headers, cells) if c]
        if pairs:
            out.append("- " + "｜".join(pairs))
    return out or block
