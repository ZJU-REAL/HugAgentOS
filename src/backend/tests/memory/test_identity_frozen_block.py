"""Behavior tests for the session-constant blocks + the sub-agent prompt date.

Background: any per-user / per-day changing bytes in the system prompt bust the
LLM prefix cache (the server-side chat template renders the tool schema after
the system text). Therefore:
- user identity and the frozen memory snapshot both go into user-role blocks
  located after the tool section, not into the system prompt;
- the date segment of the sub-agent system prompt is fixed at the very end.

Both blocks are constant for the whole chat, so sitting ahead of the history
never moves the prefix-cache boundary between turns.
"""

from __future__ import annotations

import asyncio

from core.agent_runtime.integrations.memory_identity import build_user_identity_block
from core.agent_runtime.integrations.memory_session import inject_session_blocks


def _run(coro):
    return asyncio.run(coro)


# ── inject_session_blocks combination behavior ─────────────────────────────


def test_inject_neither_returns_unchanged():
    msgs = [{"role": "user", "content": "你好"}]
    assert _run(inject_session_blocks(msgs)) is msgs


def test_inject_identity_only():
    msgs = [{"role": "user", "content": "你好"}]
    out = _run(inject_session_blocks(msgs, identity_block="## 当前用户\n- 用户名：张三"))
    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert "<session_user_identity>" in out[0]["content"]
    assert "张三" in out[0]["content"]
    assert "<session_memory_frozen>" not in out[0]["content"]
    assert out[1] is msgs[0]


def test_inject_memory_only():
    msgs = [{"role": "user", "content": "你好"}]
    out = _run(inject_session_blocks(msgs, memory_block="用户偏好：简洁回复"))
    assert len(out) == 2
    assert "<session_memory_frozen>" in out[0]["content"]
    assert "<session_user_identity>" not in out[0]["content"]


def test_inject_both_identity_precedes_memory():
    msgs = [{"role": "user", "content": "你好"}]
    out = _run(
        inject_session_blocks(
            msgs,
            identity_block="## 当前用户\n- 用户名：张三",
            memory_block="用户偏好：简洁回复",
        )
    )
    assert len(out) == 3
    assert "<session_user_identity>" in out[0]["content"]
    assert "<session_memory_frozen>" in out[1]["content"]
    assert out[0]["_context_item"]["kind"] == "identity"
    assert out[1]["_context_item"]["kind"] == "memory"
    assert out[0]["_context_item"]["trust"] == "system"
    assert out[2] is msgs[0]


def test_blocks_never_reach_the_system_slot():
    msgs = [{"role": "user", "content": "你好"}]
    out = _run(inject_session_blocks(msgs, identity_block="## 当前用户", memory_block="偏好：简洁"))
    assert all(m["role"] == "user" for m in out)


# ── build_user_identity_block short-circuit branches ───────────────────────


def test_identity_block_skips_anonymous_and_empty():
    assert _run(build_user_identity_block("")) == ""
    assert _run(build_user_identity_block("anonymous")) == ""


# ── Sub-agent system prompt: the date segment must be last ─────────────────


def test_subagent_prompt_date_is_last_segment():
    from prompts.prompt_runtime import build_subagent_system_prompt

    class _FakeAgent:
        system_prompt = "你是测试助手。"

    prompt = build_subagent_system_prompt(_FakeAgent(), tool_schemas=[], enabled_mcp_keys=[])
    assert "## 当前时间" in prompt
    last_segment = prompt.split("\n\n")[-2:]  # heading line + date line
    assert any("## 当前时间" in seg for seg in last_segment), (
        "日期段必须是 system prompt 的最后一段（前缀缓存要求），实际尾部：%r" % prompt[-200:]
    )
    # Role definition comes before the date
    assert prompt.index("你是测试助手") < prompt.index("## 当前时间")
