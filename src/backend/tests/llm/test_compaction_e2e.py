# -*- coding: utf-8 -*-
"""End-to-end test of context compaction: actually drives run_post_turn_compaction.

Uses a SQLite test DB + a fake summarizer to reproduce the original bug scenario
(gitee multi-turn collection), verifying:
- Before trigger: full tool results are in history (not truncated across turns);
- After trigger: checkpoint is persisted;
- Later turns: rebuilt from "summary + latest user messages", dropping old assistant/tool;
- Rebuilt-history token count is squeezed down; the summary carries into later turns and is not re-compacted.
"""

import contextlib

import pytest

from core.db.models import ChatSession
from core.llm import compaction as C
from core.services.chat_service import ChatService
from core.services import compaction_service as S


CHAT_ID = "chat_e2e"


@pytest.fixture
def patched_sessionlocal(db_session, monkeypatch):
    """Point the SessionLocal used inside compaction_service at the test session."""

    @contextlib.contextmanager
    def _fake_sessionlocal():
        yield db_session

    import core.db.engine as engine

    monkeypatch.setattr(engine, "SessionLocal", _fake_sessionlocal)
    return db_session


def _u(svc, i, text):
    return svc.message_repo.create(
        {"message_id": f"u{i}", "chat_id": CHAT_ID, "role": "user", "content": text}
    )


def _a(svc, i, text, tool_calls=None):
    return svc.message_repo.create(
        {
            "message_id": f"a{i}",
            "chat_id": CHAT_ID,
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
        }
    )


@pytest.mark.asyncio
async def test_full_compaction_cycle(patched_sessionlocal, monkeypatch):
    db = patched_sessionlocal
    db.add(ChatSession(chat_id=CHAT_ID, user_id="u1", title="gitee"))
    db.commit()
    svc = ChatService(db)

    # ── Reproduce the original multi-turn scenario ──
    _u(svc, 1, "在 gitee 查找 nbacheng 用户信息并插入 developer 表")
    _a(
        svc,
        1,
        "已插入 nbacheng",
        [
            {
                "tool_name": "web_fetch",
                "tool_args": {"url": "gitee.com/nbacheng"},
                "result": "示例用户 demouser 粉丝219 " + "详情" * 2000,  # oversized tool result
            }
        ],
    )
    _u(svc, 2, "查找 dromara 用户信息并插入")
    _a(
        svc,
        2,
        "已插入 dromara",
        [
            {
                "tool_name": "web_fetch",
                "tool_args": {"url": "gitee.com/dromara"},
                "result": "dromara 组织 92仓库 " + "项目" * 2000,
            }
        ],
    )

    # Before trigger: full tool results are all present (not truncated across turns)
    hist = S._load_history(svc, CHAT_ID)
    import json

    blob = json.dumps(hist, ensure_ascii=False)
    assert "详情详情" in blob and "项目项目" in blob

    # ── Fake summarizer: returns a structured handoff summary ──
    async def _fake_summarize(history, *, timeout):
        return "已完成 developer 表插入 nbacheng/dromara；待办：无；关键数据：nbacheng粉丝219"

    monkeypatch.setattr(S, "_summarize", _fake_summarize)

    # ── Drive the real compaction ──
    ok = await S.run_post_turn_compaction(CHAT_ID)
    assert ok is True

    # checkpoint persisted
    ck = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert ck is not None
    assert C.is_summary_message(ck.content)

    # ── New turn after compaction ──
    _u(svc, 3, "列出 developer 表所有记录")
    hist2 = S._load_history(svc, CHAT_ID)
    texts = [C._message_text(m.get("content")) for m in hist2]
    blob2 = "\n".join(texts)

    # 1) Old oversized tool results have been dropped (compaction took effect)
    assert "详情详情" not in blob2 and "项目项目" not in blob2
    # 2) Summary is present and carries SUMMARY_PREFIX
    assert any(C.is_summary_message(t) for t in texts)
    assert "关键数据：nbacheng粉丝219" in blob2
    # 3) Latest user message + the new turn are retained
    assert any("列出 developer 表所有记录" in t for t in texts)
    # 4) Rebuilt-history token count is squeezed down (far smaller than before compaction)
    before_tokens = C.approx_token_count(blob)
    after_tokens = C.approx_token_count(blob2)
    assert after_tokens < before_tokens // 2, (before_tokens, after_tokens)


@pytest.mark.asyncio
async def test_second_compaction_rolls_summary_not_resummarize(patched_sessionlocal, monkeypatch):
    """Second compaction: rolls forward on the previous summary, and collect_user_messages does not treat the old summary as a user message."""
    db = patched_sessionlocal
    db.add(ChatSession(chat_id=CHAT_ID, user_id="u1", title="roll"))
    db.commit()
    svc = ChatService(db)
    _u(svc, 1, "q1")
    _a(svc, 1, "a1")

    calls = {"n": 0}

    async def _fake_summarize(history, *, timeout):
        calls["n"] += 1
        # Assert: in the history entering the summarizer, the old summary is not double-counted as a user message to be compacted
        users = C.collect_user_messages(history)
        assert not any(C.is_summary_message(u) for u in users)
        return f"summary-v{calls['n']}"

    monkeypatch.setattr(S, "_summarize", _fake_summarize)

    await S.run_post_turn_compaction(CHAT_ID)
    _u(svc, 2, "q2")
    _a(svc, 2, "a2")
    await S.run_post_turn_compaction(CHAT_ID)

    ck = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert "summary-v2" in ck.content
    # Both times actually called the summarizer (rolling), without re-counting the old summary as a user message
    assert calls["n"] == 2
