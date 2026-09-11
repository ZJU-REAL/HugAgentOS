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


# ── split_history_for_compaction ─────────────────────────────────────────────


def _step(question: str, call_id: str, answer: str):
    """One user turn answered through a tool call: user → assistant step → tool row."""
    return [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": f"think about {question}"},
                {"type": "tool_call", "id": call_id, "name": "search", "input": "{}"},
            ],
        },
        {"role": "tool", "content": [{"type": "tool_result", "id": call_id, "name": "search", "output": answer}]},
    ]


def test_split_keeps_the_longest_tail_that_starts_at_a_legal_boundary():
    history = _step("q1", "c1", "r1") + _step("q2", "c2", "r2")
    tail_tokens = sum(C.approx_token_count(C.render_content_for_summary(r["content"])) for r in history[3:])

    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=tail_tokens)

    assert [r["role"] for r in older] == ["user", "assistant", "tool"]
    assert recent == history[3:]
    assert recent[0]["content"] == "q2"


def test_split_never_starts_the_tail_at_a_tool_result():
    history = _step("q1", "c1", "r1")
    only_result = C.approx_token_count(C.render_content_for_summary(history[2]["content"]))

    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=only_result)

    # The tool row alone would fit, but a tail may not begin with an orphan result.
    assert recent == []
    assert older == history


def test_split_cuts_inside_an_accumulated_assistant_row_only_at_step_starts():
    live = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "A"},
                {"type": "tool_call", "id": "c1", "name": "t", "input": "{}"},
                {"type": "tool_result", "id": "c1", "name": "t", "output": "big " * 200},
                {"type": "thinking", "thinking": "B"},
                {"type": "text", "text": "final"},
            ],
        },
    ]
    last_step = C.approx_token_count(C.render_content_for_summary(
        [{"type": "thinking", "thinking": "B"}, {"type": "text", "text": "final"}]
    ))

    older, recent = C.split_history_for_compaction(live, keep_recent_tokens=last_step)

    assert [b["type"] for b in recent[0]["content"]] == ["thinking", "text"]
    assert [r["role"] for r in older] == ["user", "assistant", "tool"]


def test_split_waits_for_pending_tool_calls_before_cutting():
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_call", "id": "c1", "name": "t", "input": "{}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "premature"}]},
        {"role": "tool", "content": [{"type": "tool_result", "id": "c1", "name": "t", "output": "r"}]},
    ]

    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=10_000)

    # Everything fits, so nothing is older; the cut can only sit where c1 is answered.
    assert older == []
    assert recent == history
    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=3)
    assert recent == []


def test_split_does_not_open_the_tail_with_a_previous_summary():
    history = [
        {"role": "user", "content": C.format_summary_text("old")},
        {"role": "user", "content": "new question"},
    ]
    older, recent = C.split_history_for_compaction(history, keep_recent_tokens=10_000)
    assert recent == [history[1]]
    assert older == [history[0]]


# ── is_summary_message ───────────────────────────────────────────────────────


def test_is_summary_message():
    assert C.is_summary_message(C.format_summary_text("x")) is True
    assert C.is_summary_message("just a normal message") is False
    # prefix only, without a newline, does not count
    assert C.is_summary_message(C.SUMMARY_PREFIX) is False


# ── build_compacted_history ──────────────────────────────────────────────────


def test_build_puts_the_summary_first_and_the_tail_verbatim():
    recent = [
        {"role": "user", "content": "kept question"},
        {"role": "assistant", "content": [{"type": "text", "text": "kept answer"}]},
    ]
    history = C.build_compacted_history("summary text", recent)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "summary text"
    assert history[0]["_context_item"]["kind"] == "compaction_summary"
    assert history[0]["_context_item"]["origin"] == "harness:compaction"
    assert history[0]["_context_item"]["trust"] == "system"
    assert history[1:] == recent


def test_build_empty_summary_fallback():
    history = C.build_compacted_history("", [])
    assert history == [history[0]]
    assert history[0]["content"] == "(no summary available)"


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
