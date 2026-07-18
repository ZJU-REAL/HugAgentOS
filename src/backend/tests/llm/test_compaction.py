# -*- coding: utf-8 -*-
"""Consistency tests for compaction.py's compaction algorithm.

Covers token estimation, middle-truncation markers, selecting the most recent user messages by budget, summary concatenation and filtering, etc.
"""

from core.llm import compaction as C


# ── token estimation (truncate.rs::approx_token_count) ────────────────────────────


def test_approx_token_count_is_ceil_bytes_over_4():
    # ascii: 8 bytes -> ceil(8/4)=2
    assert C.approx_token_count("abcdefgh") == 2
    # 7 bytes -> ceil(7/4)=2
    assert C.approx_token_count("abcdefg") == 2
    # empty -> 0
    assert C.approx_token_count("") == 0
    # Chinese: 3 bytes per character, 4 characters = 12 bytes -> 3 tokens
    assert C.approx_token_count("智能助手") == 3


def test_approx_bytes_for_tokens():
    assert C.approx_bytes_for_tokens(10) == 40


# ── collect_user_messages（compact_tests.rs）────────────────────────────────


def test_collect_user_messages_extracts_user_text_only():
    items = [
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": "first"},
        {"role": "tool", "content": "toolresult"},
    ]
    assert C.collect_user_messages(items) == ["first"]


def test_collect_user_messages_filters_summary_messages():
    summary = C.format_summary_text("prior summary body")
    items = [
        {"role": "user", "content": summary},
        {"role": "user", "content": "real user message"},
    ]
    assert C.collect_user_messages(items) == ["real user message"]


def test_collect_user_messages_handles_block_content():
    items = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ignored"}]},
    ]
    assert C.collect_user_messages(items) == ["hello"]


# ── is_summary_message ───────────────────────────────────────────────────────


def test_is_summary_message():
    assert C.is_summary_message(C.format_summary_text("x")) is True
    assert C.is_summary_message("just a normal message") is False
    # prefix only, without a newline, does not count
    assert C.is_summary_message(C.SUMMARY_PREFIX) is False


# ── build_compacted_history（compact_tests.rs）──────────────────────────────


def test_build_appends_summary_as_last_user_message():
    history = C.build_compacted_history(["first user message"], "summary text")
    assert history[-1]["role"] == "user"
    assert history[-1]["content"] == "summary text"


def test_build_empty_summary_fallback():
    history = C.build_compacted_history(["u"], "")
    assert history[-1]["content"] == "(no summary available)"


def test_build_truncates_overlong_user_message():
    # a single user message that overflows the budget should be middle-truncated and kept
    max_tokens = 16
    big = "word " * 200
    history = C.build_compacted_history([big], "SUMMARY", max_tokens=max_tokens)
    assert len(history) == 2
    truncated = history[0]["content"]
    summary = history[1]["content"]
    assert "tokens truncated" in truncated
    assert big not in truncated
    assert summary == "SUMMARY"


def test_build_selects_recent_messages_within_budget():
    # three user messages, budget 25 tokens: accumulate from the tail C(10)+B(10)=20, 5 left; the earliest A(10) overflows
    # -> truncate A to 5 tokens and keep it (not discard), then order chronologically.
    msgs = ["A" * 40, "B" * 40, "C" * 40]  # each is 40 bytes = 10 tokens
    history = C.build_compacted_history(msgs, "S", max_tokens=25)
    contents = [h["content"] for h in history]
    assert len(contents) == 4
    assert "tokens truncated" in contents[0]  # A truncated and kept
    assert contents[1] == "B" * 40
    assert contents[2] == "C" * 40
    assert contents[3] == "S"


def test_build_preserves_chronological_order():
    msgs = ["one", "two", "three"]
    history = C.build_compacted_history(msgs, "S", max_tokens=10_000)
    assert [h["content"] for h in history] == ["one", "two", "three", "S"]


# ── middle truncation (truncate.rs) ──────────────────────────────────────────────────


def test_truncate_middle_keeps_head_and_tail():
    s = "H" * 100 + "M" * 100 + "T" * 100  # 300 bytes
    out, orig = C.truncate_middle_with_token_budget(s, 20)  # 20 token = 80 bytes
    assert out.startswith("H")
    assert out.endswith("T")
    assert "tokens truncated" in out
    assert orig == C.approx_token_count(s)


def test_truncate_no_op_when_within_budget():
    s = "short"
    out, orig = C.truncate_middle_with_token_budget(s, 10_000)
    assert out == s
    assert orig is None


# ── prompts verbatim ───────────────────────────────────────────────────────────


def test_prompts_verbatim():
    assert C.SUMMARIZATION_PROMPT.startswith("You are performing a CONTEXT CHECKPOINT COMPACTION.")
    assert "Current progress and key decisions made" in C.SUMMARIZATION_PROMPT
    assert C.SUMMARY_PREFIX.startswith("Another language model started to solve this problem")
    assert C.SUMMARY_PREFIX.rstrip().endswith("assist with your own analysis:")
