# -*- coding: utf-8 -*-
"""Cross-turn integration tests for context compaction (real SQLite).

Covers: cross-turn tool results not truncated, checkpoint persistence and
consumption, UI list filtering, delete cascading.
"""

import json


from core.db.models import ChatMessage, ChatSession
from core.llm import compaction as C
from core.services.chat_service import ChatService
from core.services import compaction_service as S


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
    return svc.message_repo.create(
        {
            "message_id": f"a_{id(text)%99999}",
            "chat_id": CHAT_ID,
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
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
    user_msgs = C.collect_user_messages(history_before)
    summary_text = C.format_summary_text("【摘要】两轮已完成，待办：无")
    replacement = C.build_compacted_history(user_msgs, summary_text)
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
    monkeypatch.setattr(S, "settings", fake)
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
                {"type": "tool_call", "id": "t1", "name": "db_query", "input": '{"sql": "SELECT 1"}'},
            ],
        },
        {
            "role": "tool",
            "content": [
                {"type": "tool_result", "id": "t1", "name": "db_query", "output": "增加值 1234.5 亿元"},
            ],
        },
    ]
    flat = S._flatten_for_summary(history)
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


def test_pre_turn_compaction_fast_path_no_llm(monkeypatch):
    """Zero external overhead below threshold / with no determinable threshold: never touches the DB, never calls the LLM (protects time-to-first-token)."""
    import asyncio

    async def boom(history, *, timeout):  # noqa: ARG001
        raise AssertionError("fast path must not call the LLM")

    monkeypatch.setattr(S, "_summarize", boom)
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

    real = S.settings
    tiny = dataclasses.replace(real.compaction, token_limit=10)
    monkeypatch.setattr(S, "settings", dataclasses.replace(real, compaction=tiny))

    async def fake_summarize(history, *, timeout):  # noqa: ARG001
        return "模拟交接摘要"

    monkeypatch.setattr(S, "_summarize", fake_summarize)
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
    assert C.is_summary_message(out[-1]["content"]), "压缩后历史以摘要收尾"
    assert any(m.get("content") == "问题一" for m in out[:-1]), "最近 user 消息保留"
    assert not any(m.get("role") == "assistant" for m in out), "assistant 消息全部丢弃"

    ckpt = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert ckpt is not None, "PreTurn 必须落 checkpoint"
    extra = ckpt.extra_data or {}
    assert extra.get("notice_pending") is True
    assert extra.get("replacement_history"), "checkpoint 带 replacement_history"


def test_summarize_context_overflow_self_heal(monkeypatch):
    """Summary input exceeds the window → drop the oldest history and retry until it fits (aligned with Codex's ContextWindowExceeded handling)."""
    import asyncio

    monkeypatch.setattr(S, "_resolve_summarizer_model", lambda: ("http://fake", "k", "test-model"))
    monkeypatch.setattr(S, "_load_base_system_prompt", lambda: "SYS PROMPT")

    calls = []

    class _FakeResp:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            msgs = json["messages"]
            calls.append(list(msgs))
            if len(calls) <= 2:
                return _FakeResp(400, text="This model's maximum context length is 1000 tokens")
            return _FakeResp(200, payload={"choices": [{"message": {"content": "交接摘要"}}]})

    monkeypatch.setattr(S.httpx, "AsyncClient", _FakeClient)

    history = [{"role": "user", "content": f"历史消息{i}，" * 30} for i in range(30)]
    out = asyncio.run(S._summarize(history, timeout=5))
    assert out == "交接摘要"
    assert len(calls) == 3, "两次超窗 + 一次成功"
    assert len(calls[2]) < len(calls[0]), "重试时必须丢掉最旧消息"
    assert calls[2][0]["role"] == "system", "system 头永不丢弃"
    assert calls[2][-1]["content"] == C.SUMMARIZATION_PROMPT, "末尾摘要指令永不丢弃"
    # What gets dropped is the oldest history: the first history message should be gone
    assert "历史消息0" not in "".join(m["content"] for m in calls[2])


def test_summarize_non_context_error_no_retry(monkeypatch):
    """Non-context-overflow errors (e.g. auth failure) skip the self-heal loop; return None after one attempt."""
    import asyncio

    monkeypatch.setattr(S, "_resolve_summarizer_model", lambda: ("http://fake", "k", "test-model"))
    monkeypatch.setattr(S, "_load_base_system_prompt", lambda: "")

    calls = []

    class _FakeResp:
        status_code = 400
        text = "Invalid API key provided"

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(1)
            return _FakeResp()

    monkeypatch.setattr(S.httpx, "AsyncClient", _FakeClient)
    out = asyncio.run(S._summarize([{"role": "user", "content": "q"}], timeout=5))
    assert out is None
    assert len(calls) == 1, "非超窗错误不应重试"


def test_pre_turn_in_turn_view_keeps_current_user_last(db_session, monkeypatch):
    """In-turn consumption view: stream/reply assumes "the last user message = this turn's input" —
    after compaction, the current user message must close the list after the summary (Codex
    post-compaction shape), otherwise the summary would be mistaken for this turn's input."""
    import asyncio
    import dataclasses

    import core.db.engine as engine_mod
    from sqlalchemy.orm import sessionmaker

    _mk_session(db_session)
    svc = ChatService(db_session)
    _add_user(svc, "当前问题")

    real = S.settings
    tiny = dataclasses.replace(real.compaction, token_limit=10)
    monkeypatch.setattr(S, "settings", dataclasses.replace(real, compaction=tiny))

    async def fake_summarize(history, *, timeout):  # noqa: ARG001
        return "摘要"

    monkeypatch.setattr(S, "_summarize", fake_summarize)
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
    # The checkpoint still stores the canonical shape (summary last) for replay in later turns
    ckpt = svc.get_latest_compaction_checkpoint(CHAT_ID)
    canonical = (ckpt.extra_data or {})["replacement_history"]
    assert C.is_summary_message(canonical[-1]["content"])
