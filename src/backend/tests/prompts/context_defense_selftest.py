"""Selftest for the context-management defense layers.

Pins the invariants that workflow.py relies on:

- Layer C  when the current user message alone exceeds the budget, it is kept after head+tail truncation
- base     trim_history always keeps the last user message (force_keep anchor)
- base     multimodal list[block] is not mis-estimated via str() into an astronomical token count

No third-party dependencies; runnable directly with `python -m tests.context_defense_selftest`.

Note: overall cross-turn length is handled by the compaction mechanism (end-of-turn
compaction persisted as checkpoints + the PreTurn pre-turn fallback, see
``core/services/compaction_service.py``); ``trim_history`` is only the last line of
defense after compaction fails (pure trimming, no on-the-spot summarization — the
former Layer B ``history_summarizer`` was retired when PreTurn landed). In-turn tool
result bloat is handled by ``SafeCompressionModel`` and ``compress_in_turn_tool_results``.
"""

from __future__ import annotations

import sys
from types import ModuleType

# Before importing core.llm.*, replace chat_models / message_compat — the two modules
# that pull in heavy dependencies like httpx / agentscope — with minimal stubs, so the
# selftest can run in environments without those dependencies installed (CI, a bare
# Python install).
def _install_stubs() -> None:
    if "core.llm.chat_models" not in sys.modules:
        m = ModuleType("core.llm.chat_models")

        def _no_summarize_model():
            raise RuntimeError("chat_models stubbed for selftest")

        m.get_summarize_model = _no_summarize_model
        m.make_chat_model = lambda *a, **kw: None
        m.get_default_model = lambda *a, **kw: None
        sys.modules["core.llm.chat_models"] = m

    if "core.llm.message_compat" not in sys.modules:
        m = ModuleType("core.llm.message_compat")

        def _extract_text_from_chat_response(resp):
            content = getattr(resp, "content", None)
            if isinstance(content, str):
                return content
            return str(content) if content is not None else ""

        m.extract_text_from_chat_response = _extract_text_from_chat_response
        sys.modules["core.llm.message_compat"] = m


_install_stubs()

from core.llm.context_manager import (
    ContextBudget,
    ContextWindowManager,
    compress_oversized_user_message,
    estimate_message_tokens,
)
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# Layer C: current user message exceeds the model cap
# ---------------------------------------------------------------------------

def test_compress_oversized_user_message_string():
    huge = "Q" * 500_000
    msg = _user(huge)
    new_msg, did = compress_oversized_user_message(msg, max_tokens=10_000)
    assert did, "should be compressed"
    assert estimate_message_tokens(new_msg["content"]) <= 12_000  # 10K + some overhead
    assert "省略" in new_msg["content"]
    # Head and tail are still there
    assert new_msg["content"].startswith("Q")
    assert new_msg["content"].endswith("Q")
    # The original msg is not modified
    assert msg["content"] == huge
    print("✓ Layer C: 超大 user 字符串被头尾截断")


def test_compress_oversized_user_message_passthrough():
    msg = _user("正常长度的问题")
    new_msg, did = compress_oversized_user_message(msg, max_tokens=10_000)
    assert not did
    assert new_msg is msg, "small message must pass through by reference"
    print("✓ Layer C: 正常大小 user 消息直通")


def test_compress_oversized_user_message_multimodal():
    huge_text = "T" * 500_000
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": huge_text},
            {"type": "image", "source": {"data": "B" * 200_000}},
        ],
    }
    new_msg, did = compress_oversized_user_message(msg, max_tokens=10_000)
    assert did, "should compress the giant text block"
    # The image block is still there
    assert any(b.get("type") == "image" for b in new_msg["content"])
    # The text is truncated
    text_block = next(b for b in new_msg["content"] if b.get("type") == "text")
    assert "省略" in text_block["text"]
    assert len(text_block["text"]) < 50_000
    print("✓ Layer C: 多模态消息中只截断最大的文本 block，图片保留")


# ---------------------------------------------------------------------------
# Base: trim_history force_keep + multimodal estimation
# ---------------------------------------------------------------------------

def test_estimate_message_tokens_multimodal():
    fake_b64 = "A" * 200_000
    content = [
        {"type": "text", "text": "图片描述一下"},
        {"type": "image", "source": {"type": "base64", "data": fake_b64}},
    ]
    est = estimate_message_tokens(content)
    # The old implementation was str(content) ~= 80K tokens; the new one should be within ~1000
    assert est < 2_000, f"multimodal token estimate too high: {est}"
    print(f"✓ base: 多模态 token 估算保持低位 ({est} tokens)")


def test_estimate_message_tokens_tool_result_block():
    block = {
        "type": "tool_result",
        "content": [{"type": "text", "text": "结果数据"}],
    }
    est = estimate_message_tokens([block])
    assert 1 <= est < 100
    print(f"✓ base: tool_result block 按内部 text 估算 ({est} tokens)")


def test_trim_history_force_keep_current_user():
    mgr = ContextWindowManager(ContextBudget(model_context_window=10_000_000))
    huge_query = "Q" * 150_000  # roughly 60K tokens
    messages = [
        _user("第一轮问题"),
        _assistant("第一轮回复"),
        _user(huge_query),
    ]
    trimmed = mgr.trim_history(messages, max_tokens=50)
    assert trimmed, "must not return empty"
    assert trimmed[-1] is messages[-1], "current user message must be last"
    print("✓ base: 超大当前 user 消息被 force_keep")


def test_trim_history_drops_old_when_over_budget():
    mgr = ContextWindowManager(ContextBudget(model_context_window=10_000_000))
    # About 400 tokens per message (1000 chars / 2.5)
    big = "X" * 1_000
    messages = [
        _user(big + "Q1"),
        _assistant(big + "A1"),
        _user(big + "Q2"),
        _assistant(big + "A2"),
        _user("Q3"),  # short current-turn message
    ]
    trimmed = mgr.trim_history(messages, max_tokens=500)  # only enough for ~1 message
    # Q3 must be kept (force_keep), but earlier turns get dropped
    assert trimmed[-1] is messages[-1]
    assert len(trimmed) < len(messages), f"expected drops, got {len(trimmed)}/{len(messages)}"
    print(f"✓ base: 预算不足时丢老轮次但保 Q3 ({len(trimmed)}/{len(messages)})")


# ---------------------------------------------------------------------------
# Integration：manage_context = C + base
# ---------------------------------------------------------------------------

def test_manage_context_full_pipeline():
    # Build a message sequence that triggers C + base
    mgr = ContextWindowManager(ContextBudget(model_context_window=20_000))
    huge_current_user = "C" * 60_000  # roughly 24K tokens, above the Layer C threshold of 20K * 0.6 = 12K
    messages = [
        _user("Q1"),
        _assistant("A1"),
        _user("Q2"),
        _assistant("A2"),
        _user(huge_current_user),
    ]
    kept, dropped = mgr.manage_context(messages)
    assert kept, "kept must be non-empty"
    assert kept[-1].get("role") == "user", "kept must end with user message"

    # Layer C kicks in: the current user message is compressed
    current_user = kept[-1]["content"]
    assert len(current_user) < 40_000, "current user message should be compressed"
    assert "省略" in current_user
    print(f"✓ integration: manage_context 两层一次性跑通 (kept={len(kept)}, dropped={len(dropped)})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    # Layer C
    test_compress_oversized_user_message_string()
    test_compress_oversized_user_message_passthrough()
    test_compress_oversized_user_message_multimodal()

    # Base
    test_estimate_message_tokens_multimodal()
    test_estimate_message_tokens_tool_result_block()
    test_trim_history_force_keep_current_user()
    test_trim_history_drops_old_when_over_budget()

    # Integration
    test_manage_context_full_pipeline()

    print("\n=== context_defense_selftest: OK ===")


if __name__ == "__main__":
    main()
