# -*- coding: utf-8 -*-
"""Pure helpers for compacting history into recent user messages plus a summary."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from core.llm.context_ir import (
    KIND_COMPACTION,
    SESSION_CONTEXT_META_KEY,
    make_text_context_item,
)
from core.llm.model_steps import explode_history_rows

# ── Constants ────────────────────────────────────────────────────────────────


class CompactionPhase(str, Enum):
    """Where the shared compaction pipeline was triggered.

    - :attr:`MID_TURN` — the main path. Checked at every ReAct step boundary
      by ``CompactingAgent.compress_context``.
    - :attr:`PRE_TURN` — safety net before a turn starts sampling, for history
      that was already over the threshold when loaded.
    - :attr:`POST_TURN` — background pre-warm after the stream closes.
    """

    MID_TURN = "mid_turn"
    PRE_TURN = "pre_turn"
    POST_TURN = "post_turn"


# Token estimation: roughly 1 token per 4 utf-8 bytes
APPROX_BYTES_PER_TOKEN = 4

# Tokens of recent history kept verbatim after compaction (the default for
# ``CompactionSettings.keep_recent_tokens``).
COMPACT_KEEP_RECENT_TOKENS = 20_000

# Marker stored on internal checkpoint messages.
COMPACTION_CHECKPOINT_KIND = "compaction_summary"

# Shared handoff instruction. The final line preserves the conversation language.
SUMMARIZATION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary "
    "for another LLM that will resume the task.\n\n"
    "Include:\n"
    "- Current progress and key decisions made\n"
    "- Important context, constraints, or user preferences\n"
    "- What remains to be done (clear next steps)\n"
    "- Any critical data, examples, or references needed to continue\n\n"
    "Be concise, structured, and focused on helping the next LLM seamlessly continue the work.\n"
    "Write the summary in the same language as the conversation.\n"
)

# Marker that identifies a handoff summary during replay and rolling compaction.
SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of "
    "its thinking process. You also have access to the state of the tools that were "
    "used by that language model. Use this to build on the work that has already been "
    "done and avoid duplicating work. Here is the summary produced by the other "
    "language model, use the information in this summary to assist with your own analysis:"
)


# ── Token estimation / middle truncation ─────────────────────────────────────


def approx_token_count(text: str) -> int:
    """Approximate token count = ceil(utf-8 byte count / 4)."""
    n = len(text.encode("utf-8"))
    return (n + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def approx_bytes_for_tokens(tokens: int) -> int:
    """Convert a token count into a rough byte budget: tokens * 4."""
    return tokens * APPROX_BYTES_PER_TOKEN


def approx_tokens_from_byte_count(nbytes: int) -> int:
    """Convert a byte count into an approximate token count: ceil(bytes / 4); returns 0 for non-positive values."""
    if nbytes <= 0:
        return 0
    return (nbytes + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def _format_truncation_marker(use_tokens: bool, removed_count: int) -> str:
    if use_tokens:
        return f"…{removed_count} tokens truncated…"
    return f"…{removed_count} chars truncated…"


def _removed_units(use_tokens: bool, removed_bytes: int, removed_chars: int) -> int:
    return approx_tokens_from_byte_count(removed_bytes) if use_tokens else removed_chars


def _split_budget(budget: int) -> Tuple[int, int]:
    left = budget // 2
    return left, budget - left


def _split_string(s: str, beginning_bytes: int, end_bytes: int) -> Tuple[int, str, str]:
    """Keep head/tail within a utf-8 byte budget (char boundaries); returns ``(removed_chars, before, after)``."""
    if not s:
        return 0, "", ""
    b = s.encode("utf-8")
    total = len(b)
    tail_start_target = total - end_bytes if total > end_bytes else 0
    prefix_end = 0
    suffix_start = total
    removed_chars = 0
    suffix_started = False
    idx = 0
    for ch in s:
        char_end = idx + len(ch.encode("utf-8"))
        if char_end <= beginning_bytes:
            prefix_end = char_end
        elif idx >= tail_start_target:
            if not suffix_started:
                suffix_start = idx
                suffix_started = True
        else:
            removed_chars += 1
        idx = char_end
    if suffix_start < prefix_end:
        suffix_start = prefix_end
    before = b[:prefix_end].decode("utf-8", errors="ignore")
    after = b[suffix_start:].decode("utf-8", errors="ignore")
    return removed_chars, before, after


def _truncate_with_byte_estimate(s: str, max_bytes: int, use_tokens: bool) -> str:
    if not s:
        return ""
    total_chars = len(s)
    total_bytes = len(s.encode("utf-8"))
    if max_bytes == 0:
        return _format_truncation_marker(
            use_tokens, _removed_units(use_tokens, total_bytes, total_chars)
        )
    if total_bytes <= max_bytes:
        return s
    left_budget, right_budget = _split_budget(max_bytes)
    removed_chars, left, right = _split_string(s, left_budget, right_budget)
    marker = _format_truncation_marker(
        use_tokens, _removed_units(use_tokens, total_bytes - max_bytes, removed_chars)
    )
    return f"{left}{marker}{right}"


def truncate_middle_with_token_budget(s: str, max_tokens: int) -> Tuple[str, Optional[int]]:
    """Middle-truncate to ``max_tokens``, keeping head and tail. Returns (possibly truncated string, original token count or None)."""
    if not s:
        return "", None
    if max_tokens > 0 and len(s.encode("utf-8")) <= approx_bytes_for_tokens(max_tokens):
        return s, None
    truncated = _truncate_with_byte_estimate(s, approx_bytes_for_tokens(max_tokens), True)
    if truncated == s:
        return truncated, None
    return truncated, approx_token_count(s)


def truncate_text_tokens(content: str, max_tokens: int) -> str:
    """Middle-truncate within a token budget; returns only the truncated text."""
    return truncate_middle_with_token_budget(content, max_tokens)[0]


# ── Message text extraction ──────────────────────────────────────────────────


def _message_text(content: Any) -> str:
    """Extract plain text from a message's content (str or block list): concatenate input/output text, ignore images."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    pieces.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("output") or ""
                if t and item.get("type") in (None, "text", "input_text", "output_text"):
                    pieces.append(str(t))
        return "\n".join(pieces)
    return str(content)


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(str(text) if text is not None else str(item))
            elif isinstance(item, str):
                parts.append(item)
            else:
                text = getattr(item, "text", None)
                parts.append(str(text) if text is not None else str(item))
        return "\n".join(parts)
    return str(output) if output is not None else ""


def render_content_for_summary(content: Any) -> str:
    """Render one message's content into the full text a summary model reads.

    Tool calls and results are kept as labelled structured text — the summary
    sees everything the turn did — and reasoning is labelled the way Pi marks
    ``[Assistant thinking]``: explicit summary material, never replayed as an
    answer. The same rendering drives token estimates for history.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    pieces: List[str] = []
    for item in content:
        if isinstance(item, str):
            if item:
                pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype in (None, "text", "input_text", "output_text"):
            t = item.get("text") or item.get("output") or ""
            if t:
                pieces.append(str(t))
        elif btype == "thinking":
            t = item.get("thinking") or ""
            if t:
                pieces.append(f"[thinking]\n{t}")
        elif btype in ("tool_call", "tool_use"):
            name = item.get("name") or "unknown_tool"
            args = item.get("input") or ""
            pieces.append(f"[tool_call {name}] arguments: {args}")
        elif btype == "tool_result":
            name = item.get("name") or "unknown_tool"
            out = item.get("output")
            if out is None:
                out = item.get("content", "")
            pieces.append(f"[tool_result {name}]\n{_tool_output_text(out)}")
    return "\n".join(pieces)


def is_summary_message(text: str) -> bool:
    """Anything starting with ``SUMMARY_PREFIX\n`` is a compaction summary."""
    return text.startswith(SUMMARY_PREFIX + "\n")


def _is_summary_row(row: Dict[str, Any]) -> bool:
    meta = row.get(SESSION_CONTEXT_META_KEY)
    if isinstance(meta, dict) and meta.get("kind") == KIND_COMPACTION:
        return True
    return row.get("role") == "user" and is_summary_message(_message_text(row.get("content")))


# ── Region selection ─────────────────────────────────────────────────────────


def _row_tokens(row: Dict[str, Any]) -> int:
    return approx_token_count(render_content_for_summary(row.get("content")))


def _tool_call_ids(row: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    calls: List[str] = []
    results: List[str] = []
    content = row.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_call", "tool_use"):
                calls.append(str(block.get("id") or ""))
            elif block.get("type") == "tool_result":
                results.append(str(block.get("id") or ""))
    return calls, results


def split_history_for_compaction(
    history: List[Dict[str, Any]],
    *,
    keep_recent_tokens: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(older, recent)``: what gets summarized and what stays verbatim.

    History is first exploded into one row per ReAct step. The recent region is
    the longest tail that fits ``keep_recent_tokens`` and starts at a legal
    boundary: a user message (not a previous summary) or the start of an
    assistant step, never a tool result — and only where every tool call
    issued before the boundary has already received its result. A step is
    therefore either wholly summarized or wholly kept; the model never sees
    half of one. When no tail fits, everything is summarized.
    """
    rows = explode_history_rows(history)
    if not rows:
        return [], []
    budget = max(0, int(keep_recent_tokens))

    # balanced_before[i]: every tool call in rows[:i] is answered in rows[:i]
    balanced_before: List[bool] = []
    pending: set[str] = set()
    for row in rows:
        balanced_before.append(not pending)
        calls, results = _tool_call_ids(row)
        pending.update(calls)
        pending.difference_update(results)
    balanced_before.append(not pending)

    tokens_from: List[int] = [0] * (len(rows) + 1)
    for index in range(len(rows) - 1, -1, -1):
        tokens_from[index] = tokens_from[index + 1] + _row_tokens(rows[index])

    cut = len(rows)
    for index in range(len(rows)):
        row = rows[index]
        role = str(row.get("role") or "user")
        if role == "tool":
            continue
        if role in ("user", "human") and _is_summary_row(row):
            continue
        if not balanced_before[index]:
            continue
        if tokens_from[index] <= budget:
            cut = index
            break
    return rows[:cut], rows[cut:]


# ── Building the compacted history ───────────────────────────────────────────


def format_summary_text(summary_suffix: str) -> str:
    """Add the prefix marker to the summary body: ``SUMMARY_PREFIX + "\\n" + summary_suffix``."""
    return f"{SUMMARY_PREFIX}\n{summary_suffix}"


def build_compacted_history(
    summary_text: str,
    recent: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Summary first, then the recent region exactly as it happened.

    The summary is one user-role message carrying explicit compaction
    provenance; the rows after it are the verbatim tail chosen by
    :func:`split_history_for_compaction`, so the model resumes from complete
    steps rather than from a digest of everything.
    """
    summary = summary_text if summary_text else "(no summary available)"
    summary_item = make_text_context_item(
        summary,
        item_id="compaction:summary",
        kind=KIND_COMPACTION,
        origin="harness:compaction",
        trust="system",
        created_seq=0,
        priority=850,
        token_budget=20_000,
        cache_class="checkpoint",
    )
    history: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": summary,
            SESSION_CONTEXT_META_KEY: summary_item.to_manifest(),
        }
    ]
    history.extend(dict(row) for row in recent)
    return history
