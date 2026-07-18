# -*- coding: utf-8 -*-
"""Context compaction.

Across turns, compacts the conversation history into "recent user messages + summary";
the summary is persisted and carried into subsequent turns, replacing the previous
"AgentScope in-turn compaction + summary not persisted" approach. Key points:

1. Compacted history = ``[recent user messages (≤20k tokens)] + [summary (encoded as a
   user message, placed at the end)]``; **all assistant messages, tool calls, tool
   results, and earlier user messages are dropped**.
2. The summary is marked with the :data:`SUMMARY_PREFIX` prefix, recognizable by
   :func:`is_summary_message`, so it is **carried into subsequent turns without being
   re-compacted**.
3. Token estimation uses utf-8 bytes (``APPROX_BYTES_PER_TOKEN=4``); middle truncation
   keeps head + tail, marked ``…{n} tokens truncated…``.

Not truncating cross-turn tool results is the replay layer's job
(``api/routes/v1/chats.py``); this module only handles compaction itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ── Constants ────────────────────────────────────────────────────────────────

# Token estimation: roughly 1 token per 4 utf-8 bytes
APPROX_BYTES_PER_TOKEN = 4

# Token cap for the recent user messages kept after compaction
COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000

# Marker of the compaction checkpoint in chat_messages (role='system' + extra_data.kind).
# The summary is persisted and carried into subsequent turns; no more re-compacting from
# the raw history every turn.
COMPACTION_CHECKPOINT_KIND = "compaction_summary"

# Summary-generation instruction (asks the model to produce a structured handoff summary).
# The body is verbatim-identical to the Codex CLI template; the final language-following
# sentence is a deliberate addition of this repo — user conversations are almost entirely
# Chinese, and the summary must follow the conversation language, avoiding the small
# chance of an English summary affecting subsequent turns.
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

# Summary prefix: tells the next turn's model "this is the handoff summary of the prior conversation; continue from it, do not duplicate work"
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


# ── Message text extraction / filtering ──────────────────────────────────────


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


def is_summary_message(text: str) -> bool:
    """Anything starting with ``SUMMARY_PREFIX\\n`` is a compaction summary."""
    return text.startswith(SUMMARY_PREFIX + "\n")


def collect_user_messages(messages: List[Dict[str, Any]]) -> List[str]:
    """Take user text only, excluding summary messages.

    The system prompt is injected separately at the agent layer and is **not** in the DB
    messages, so no filtering for it is needed here.
    """
    out: List[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        text = _message_text(m.get("content"))
        if not text or is_summary_message(text):
            continue
        out.append(text)
    return out


# ── Building the compacted history ───────────────────────────────────────────


def format_summary_text(summary_suffix: str) -> str:
    """Add the prefix marker to the summary body: ``SUMMARY_PREFIX + "\\n" + summary_suffix``."""
    return f"{SUMMARY_PREFIX}\n{summary_suffix}"


def build_compacted_history(
    user_messages: List[str],
    summary_text: str,
    *,
    initial_context: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> List[Dict[str, Any]]:
    """Build the compacted history.

    Accumulate user messages from the tail up to ``max_tokens`` (the overflowing one gets
    middle-truncated), then order chronologically, and finally append the summary
    (encoded as a **user** message). Returns ``[{"role","content"}...]``.
    """
    history: List[Dict[str, Any]] = list(initial_context or [])

    selected: List[str] = []
    if max_tokens > 0:
        remaining = max_tokens
        for msg in reversed(user_messages):
            if remaining == 0:
                break
            tokens = approx_token_count(msg)
            if tokens <= remaining:
                selected.append(msg)
                remaining -= tokens
            else:
                selected.append(truncate_text_tokens(msg, remaining))
                break
        selected.reverse()

    for msg in selected:
        history.append({"role": "user", "content": msg})

    summary = summary_text if summary_text else "(no summary available)"
    history.append({"role": "user", "content": summary})
    return history
