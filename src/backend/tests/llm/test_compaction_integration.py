# -*- coding: utf-8 -*-
"""Cross-turn integration tests for context compaction (real SQLite).

Covers: cross-turn tool results not truncated, checkpoint persistence and
consumption, UI list filtering, delete cascading.
"""

import json
from datetime import timezone

from core.config import settings as config_settings
from core.db.models import ChatMessage, ChatSession
from core.llm.context import compaction as C
from core.models.completions.single_turn import Endpoint
from core.llm.context import budget as budget
from core.chat.compaction import service as S
from core.chat.service import ChatService


def _endpoint():
    """压缩用的摘要端点；线路钉在 chat 上，这些用例验的是超窗自救不是协议。"""
    return Endpoint(
        base_url="http://fake",
        api_key="k",
        model_name="test-model",
        api_protocol="chat_completions",
    )


CHAT_ID = "chat_compact_test"


def _mk_session(db):
    db.add(ChatSession(chat_id=CHAT_ID, user_id="u1", title="t"))
    db.commit()


def _add_user(svc, text):
    return svc.message_repo.create(
        {
            "message_id": f"u_{text[:6]}_{id(text)%9999}",
            "chat_id": CHAT_ID,
            "role": "user",
            "content": text,
        }
    )


def _add_assistant(svc, text, tool_calls=None):
    from core.llm.runtime.model_steps import record_assistant_step, record_tool_result_step

    steps = []
    for index, call in enumerate(tool_calls or []):
        call_id = f"call-{index}"
        steps.append(
            record_assistant_step(
                [
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "name": call["tool_name"],
                        "input": json.dumps(call["tool_args"]),
                    }
                ],
                provider="p",
                model="m",
                protocol="openai_chat",
            )
        )
        steps.append(
            record_tool_result_step(
                {
                    "type": "tool_result",
                    "id": call_id,
                    "name": call["tool_name"],
                    "output": call["result"],
                    "state": "success",
                },
            )
        )
    steps.append(
        record_assistant_step(
            [{"type": "text", "text": text}],
            provider="p",
            model="m",
            protocol="openai_chat",
        )
    )
    return svc.message_repo.create(
        {
            "message_id": f"a_{id(text)%99999}",
            "chat_id": CHAT_ID,
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
            "model_steps": steps,
        }
    )


def test_cross_turn_tool_results_not_truncated(db_session):
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "查一下 nbacheng")
    big_result = "X" * 8000  # far beyond the old 1000-char truncation
    tool_calls = [
        {
            "tool_name": "web_fetch",
            "tool_args": {"url": "https://gitee.com/nbacheng"},
            "result": big_result,
        }
    ]
    _add_assistant(svc, "已获取", tool_calls)
    history = S._load_history(svc, CHAT_ID)
    # Join the whole history text; verify the full 8000-char tool result was not truncated to 1000
    blob = json.dumps(history, ensure_ascii=False)
    assert big_result in blob, "跨轮工具结果应完整保留（不截断）"


def test_checkpoint_replaces_prehistory_and_keeps_tail(db_session):
    _mk_session(db_session)
    svc = ChatService(db_session)
    # Several turns before compaction
    _add_user(svc, "第一轮问题")
    _add_assistant(svc, "第一轮回答")
    _add_user(svc, "第二轮问题")
    _add_assistant(svc, "第二轮回答")
    # Simulate one compaction: collect + build + persist checkpoint (summary uses mock text)
    history_before = S._load_history(svc, CHAT_ID)
    _older, recent = C.split_history_for_compaction(history_before, keep_recent_tokens=0)
    summary_text = C.format_summary_text("【摘要】两轮已完成，待办：无")
    replacement = C.build_compacted_history(summary_text, recent)
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=summary_text,
        replacement_history=replacement,
    )

    # A new turn after compaction
    _add_user(svc, "第三轮问题")
    history = S._load_history(svc, CHAT_ID)

    # 1) Baseline = replacement_history (with summary); the original assistant body is no longer included
    assert any(C.is_summary_message(C._message_text(m.get("content"))) for m in history)
    assert not any(
        "第一轮回答" in C._message_text(m.get("content")) for m in history
    ), "压缩后应丢弃压缩点之前的 assistant 正文"
    # 2) The new post-compaction user message is kept
    assert any("第三轮问题" in C._message_text(m.get("content")) for m in history)
    # 3) The summary sits at the end of the baseline, before the third turn
    texts = [C._message_text(m.get("content")) for m in history]
    summary_idx = next(i for i, t in enumerate(texts) if C.is_summary_message(t))
    third_idx = next(i for i, t in enumerate(texts) if "第三轮问题" in t)
    assert summary_idx < third_idx


def test_checkpoint_excluded_from_user_facing_list(db_session):
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "hi")
    _add_assistant(svc, "hello")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("s"),
        replacement_history=[
            {"role": "user", "content": "hi"},
            {"role": "user", "content": C.format_summary_text("s")},
        ],
    )
    msgs, total = svc.message_repo.list_by_chat(CHAT_ID)
    roles = [m.role for m in msgs]
    assert "system" not in roles, "checkpoint(role=system) 不应出现在用户可见列表"
    assert total == 2  # only user + assistant


def test_latest_checkpoint_wins(db_session):
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "q1")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("old"),
        replacement_history=[{"role": "user", "content": C.format_summary_text("old")}],
    )
    _add_user(svc, "q2")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("new"),
        replacement_history=[{"role": "user", "content": C.format_summary_text("new")}],
    )
    ck = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert "new" in ck.content


def test_disabled_falls_back_to_full_replay(db_session, monkeypatch):
    import dataclasses

    from core.config.settings import settings as real

    disabled = dataclasses.replace(real.compaction, enabled=False)
    fake = dataclasses.replace(real, compaction=disabled)
    monkeypatch.setattr(config_settings, "settings", fake)
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "q1")
    _add_assistant(svc, "a1")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("s"),
        replacement_history=[{"role": "user", "content": "SHOULD_NOT_APPEAR"}],
    )
    history = S._load_history(svc, CHAT_ID)
    blob = json.dumps(history, ensure_ascii=False)
    assert "SHOULD_NOT_APPEAR" not in blob, "关闭开关时应忽略 checkpoint，全量重放"
    assert "a1" in blob


def test_summary_flatten_preserves_tool_blocks():
    """Summary input keeps tool blocks: tool_call / tool_result are rendered as structured text (aligned with Codex)."""
    history = [
        {"role": "user", "content": "查数据"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "好的"},
                {
                    "type": "tool_call",
                    "id": "t1",
                    "name": "db_query",
                    "input": '{"sql": "SELECT 1"}',
                },
            ],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "id": "t1",
                    "name": "db_query",
                    "output": "增加值 1234.5 亿元",
                },
            ],
        },
    ]
    from core.chat.compaction.summary import _flatten_for_summary

    flat = _flatten_for_summary(history)
    assert len(flat) == 3
    assert "[tool_call db_query]" in flat[1]["content"]
    assert '"SELECT 1"' in flat[1]["content"]
    assert flat[2]["role"] == "user"  # OpenAI-compatible endpoints don't accept the tool role
    assert "[tool_result db_query]" in flat[2]["content"]
    assert "1234.5" in flat[2]["content"]


def test_compaction_notice_popped_exactly_once(db_session):
    """Checkpoint is written with notice_pending; first pop returns True and clears the flag, second pop returns False."""
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "q1")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("s"),
        replacement_history=[{"role": "user", "content": "q1"}],
    )
    assert S.pop_compaction_notice(svc, CHAT_ID) is True
    assert S.pop_compaction_notice(svc, CHAT_ID) is False, "通知只发一次"
    # A chat without a checkpoint is always False
    assert S.pop_compaction_notice(svc, "chat_no_ckpt") is False


def test_context_state_uses_compacted_baseline_and_visible_boundary(db_session):
    """The UI receives replacement tokens plus an exact visible-message boundary."""
    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "第一轮问题")
    covered = _add_assistant(svc, "第一轮回答")
    replacement = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "user", "content": C.format_summary_text("第一轮摘要")},
    ]
    checkpoint = svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("第一轮摘要"),
        replacement_history=replacement,
    )
    _add_user(svc, "压缩后的新问题")

    from core.chat.compaction.usage_projection import get_chat_context_state

    state = get_chat_context_state(svc, CHAT_ID)["context_compaction"]

    state.pop("context_usage", None)
    assert state == {
        "checkpoint_id": checkpoint.message_id,
        "checkpoint_created_at": checkpoint.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "covered_through_message_id": covered.message_id,
        "covered_through_chat_seq": covered.chat_seq,
        "covered_message_count": 2,
        "replacement_tokens": budget.estimate_history_tokens(replacement),
    }


def test_checkpoint_tail_uses_sequence_not_identical_timestamp(db_session):
    """A post-watermark message survives even when timestamps are identical."""
    from datetime import datetime

    _mk_session(db_session)
    svc = ChatService(db_session)
    covered = _add_user(svc, "covered")
    checkpoint = svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("baseline"),
        replacement_history=[{"role": "user", "content": C.format_summary_text("baseline")}],
    )
    tail = _add_user(svc, "tail")
    same_time = datetime(2026, 1, 1, 12, 0, 0)
    db_session.query(ChatMessage).filter(
        ChatMessage.message_id.in_([covered.message_id, checkpoint.message_id, tail.message_id])
    ).update({"created_at": same_time}, synchronize_session=False)
    db_session.commit()

    history = S._load_history(svc, CHAT_ID)
    rendered = json.dumps(history, ensure_ascii=False)
    assert "baseline" in rendered
    assert "tail" in rendered
    assert (checkpoint.extra_data or {})["covers_up_to_chat_seq"] == covered.chat_seq


def test_pre_turn_compaction_fast_path_no_llm(monkeypatch):
    """Zero external overhead below threshold / with no determinable threshold: never touches the DB, never calls the LLM (protects time-to-first-token)."""
    import asyncio

    async def boom(history, *, timeout):  # noqa: ARG001
        raise AssertionError("fast path must not call the LLM")

    monkeypatch.setattr('core.chat.compaction.summary._summarize', boom)
    history = [{"role": "user", "content": "短问题"}]
    # Unknown model → window None → threshold None → fast path
    out, compacted = asyncio.run(
        S.maybe_run_pre_turn_compaction("chat_x", history, model_name="unknown-model")
    )
    assert compacted is False and out is history
    # No chat_id / empty history takes the fast path too
    assert asyncio.run(S.maybe_run_pre_turn_compaction(None, history, model_name="m"))[1] is False
    assert asyncio.run(S.maybe_run_pre_turn_compaction("chat_x", [], model_name="m"))[1] is False


def test_pre_turn_compaction_triggers_and_writes_checkpoint(db_session, monkeypatch):
    """Above threshold → synchronous compaction: returns "recent user + summary" and writes a checkpoint with notice_pending."""
    import asyncio
    import dataclasses

    import core.db.engine as engine_mod
    from sqlalchemy.orm import sessionmaker

    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "问题一")

    real = config_settings.settings
    tiny = dataclasses.replace(real.compaction, token_limit=10, keep_recent_tokens=0)
    monkeypatch.setattr(config_settings, "settings", dataclasses.replace(real, compaction=tiny))

    async def fake_summarize(history, *, timeout):  # noqa: ARG001
        return "模拟交接摘要"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', fake_summarize)
    # Checkpoint persistence goes through the global SessionLocal → point it at the test DB
    monkeypatch.setattr(engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    history = [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "很长的回答" * 200},
    ]
    # Pass context_window explicitly: resolve no longer has a default fallback
    # (unconfigured models raise), the test model isn't in the Config settings,
    # so per the new contract the caller supplies the window.
    out, compacted = asyncio.run(
        S.maybe_run_pre_turn_compaction(
            CHAT_ID, history, model_name="any-model", context_window=128_000
        )
    )
    assert compacted is True
    # Nothing is kept verbatim here (keep_recent_tokens=0): the whole region
    # became the summary, and the turn's own input is an assistant row, so the
    # in-turn view is the summary alone.
    assert [C.is_summary_message(m["content"]) for m in out] == [True]
    assert not any(m.get("role") == "assistant" for m in out), "assistant 消息全部进入摘要"

    ckpt = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert ckpt is not None, "PreTurn 必须落 checkpoint"
    extra = ckpt.extra_data or {}
    assert extra.get("notice_pending") is True
    assert extra.get("replacement_history"), "checkpoint 带 replacement_history"


def test_pre_turn_in_turn_view_keeps_current_user_last(db_session, monkeypatch):
    """In-turn consumption view: stream/reply assumes "the last user message = this turn's input" —
    after compaction, the current user message must close the list after the summary (Codex
    post-compaction shape), otherwise the summary would be mistaken for this turn's input.
    """
    import asyncio
    import dataclasses

    import core.db.engine as engine_mod
    from sqlalchemy.orm import sessionmaker

    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "旧问题" * 100)
    _add_assistant(svc, "旧回答" * 100)
    _add_user(svc, "当前问题")

    real = config_settings.settings
    tiny = dataclasses.replace(real.compaction, token_limit=10, keep_recent_tokens=10)
    monkeypatch.setattr(config_settings, "settings", dataclasses.replace(real, compaction=tiny))

    async def fake_summarize(history, *, timeout):  # noqa: ARG001
        return "摘要"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', fake_summarize)
    monkeypatch.setattr(engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    history = [
        {"role": "user", "content": "旧问题" * 100},
        {"role": "assistant", "content": "旧回答" * 100},
        {"role": "user", "content": "当前问题"},
    ]
    out, compacted = asyncio.run(
        S.maybe_run_pre_turn_compaction(
            CHAT_ID, history, model_name="any-model", context_window=128_000
        )
    )
    assert compacted is True
    assert out[-1] == {"role": "user", "content": "当前问题"}, "本轮输入必须收尾"
    assert C.is_summary_message(out[-2]["content"]), "摘要紧邻其前"
    assert sum(1 for m in out if m.get("content") == "当前问题") == 1, "本轮消息不得重复"
    # The checkpoint stores the same shape: summary first, the kept tail after it.
    ckpt = svc.get_latest_compaction_checkpoint(CHAT_ID)
    canonical = (ckpt.extra_data or {})["replacement_history"]
    assert C.is_summary_message(canonical[0]["content"])
    assert canonical[-1] == {"role": "user", "content": "当前问题"}
