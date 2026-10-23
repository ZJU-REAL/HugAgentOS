"""First-message titles use an independent call and respect later user edits."""

import asyncio
from types import SimpleNamespace

import core.chat.runtime.admission as runtime_admission
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatSession, UserShadow
from core.chat import titles as chat_titles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'titles.db'}")
    Base.metadata.create_all(engine, tables=[UserShadow.__table__, ChatSession.__table__, ChatMessage.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(chat_titles.db_engine, "SessionLocal", factory)
    with factory() as db:
        db.add(UserShadow(user_id="alice", username="alice"))
        db.add(ChatSession(chat_id="chat", user_id="alice", title="临时标题"))
        db.add(ChatMessage(message_id="first", chat_id="chat", chat_seq=1,
                           role="user", content="请分析本季度销售数据"))
        db.commit()
    yield factory
    engine.dispose()


def model(monkeypatch, generate):
    monkeypatch.setattr(chat_titles, "get_summarizer", lambda: SimpleNamespace(
        enabled=True, summarize_conversation=generate))


def test_first_question_starts_without_assistant_and_only_once(env, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            assert messages == [{"role": "user", "content": "请分析本季度销售数据"}]
            entered.set()
            await release.wait()
            return "季度销售分析"
        model(monkeypatch, generate)
        task = chat_titles.schedule_first_title("chat", "alice", "run")
        assert task is not None
        await asyncio.wait_for(entered.wait(), 2)
        with env() as db:
            row = db.get(ChatSession, "chat")
            assert row.title == "临时标题"
            assert row.extra_data["title_generation"]["status"] == "pending"
        assert chat_titles.schedule_first_title("chat", "alice", "run") is None
        release.set()
        await task
        with env() as db:
            row = db.get(ChatSession, "chat")
            assert row.title == "季度销售分析"
            assert row.extra_data["title_generation"]["status"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("manual_title", ["我的命名", "临时标题"])
def test_manual_rename_wins_even_if_model_is_in_flight(env, monkeypatch, manual_title):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            entered.set()
            await release.wait()
            return "不能覆盖用户"
        model(monkeypatch, generate)
        task = chat_titles.schedule_first_title("chat", "alice", "run")
        await asyncio.wait_for(entered.wait(), 2)
        with env() as db:
            row = db.get(ChatSession, "chat")
            row.title = manual_title
            row.extra_data = {**row.extra_data, "title_manually_set": True}
            db.commit()
        release.set()
        await task
        with env() as db:
            assert db.get(ChatSession, "chat").title == manual_title
    asyncio.run(scenario())


def test_failed_generation_keeps_fallback_and_does_not_retry(env, monkeypatch):
    async def scenario():
        async def generate(messages):
            raise RuntimeError("model unavailable")
        model(monkeypatch, generate)
        await chat_titles.schedule_first_title("chat", "alice", "run")
        with env() as db:
            row = db.get(ChatSession, "chat")
            assert row.title == "临时标题"
            assert row.extra_data["title_generation"]["status"] == "failed"
        assert chat_titles.schedule_first_title("chat", "alice", "later") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["other-user", "second-message", "fork", "manual"])
def test_ineligible_conversations_never_call_model(env, monkeypatch, case):
    model(monkeypatch, None)
    with env() as db:
        row = db.get(ChatSession, "chat")
        if case == "second-message":
            db.add(ChatMessage(message_id="next", chat_id="chat", chat_seq=2,
                               role="user", content="继续"))
        elif case == "fork":
            row.extra_data = {"fork": {"source_chat_id": "parent"}}
        elif case == "manual":
            row.extra_data = {"title_manually_set": True}
        db.commit()
    assert chat_titles.schedule_first_title("chat", "bob" if case == "other-user" else "alice", "run") is None


def test_changed_title_is_preserved_even_if_an_old_writer_loses_the_manual_flag(env, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            entered.set()
            await release.wait()
            return "模型标题"
        model(monkeypatch, generate)
        task = chat_titles.schedule_first_title("chat", "alice", "run")
        await asyncio.wait_for(entered.wait(), 2)
        with env() as db:
            row = db.get(ChatSession, "chat")
            row.title = "人工标题"
            db.commit()
        release.set()
        await task
        with env() as db:
            assert db.get(ChatSession, "chat").title == "人工标题"
    asyncio.run(scenario())


def test_metadata_patch_refreshes_stale_state_and_keeps_a_manual_title(env, monkeypatch):
    from core.chat.service import ChatService
    from core.chat.operations import sessions
    from core.chat.operations.schemas import UpdateChatRequest

    # Use the public session operation with a session that cached old metadata.
    with env() as stale:
        stale_row = stale.get(ChatSession, "chat")
        assert not (stale_row.extra_data or {}).get("title_manually_set")
        with env() as fresh:
            row = fresh.get(ChatSession, "chat")
            row.title = "人工标题"
            row.extra_data = {"title_manually_set": True}
            fresh.commit()
        monkeypatch.setattr(ChatService, "get_session_with_access",
                            lambda self, cid, uid: (self.db.get(ChatSession, cid), "admin"))
        monkeypatch.setattr(sessions, 'session_view_for_user',
                            lambda db, row, uid, level: {"title": row.title, "metadata": row.extra_data})
        def update(self, cid, fields, **kwargs):
            row = self.db.get(ChatSession, cid)
            row.extra_data = {**(row.extra_data or {}), **fields.get("extra_data", {})}
            self.db.commit()
        monkeypatch.setattr(ChatService, "update_session_fields", update)
        result = sessions.update_chat("chat", UpdateChatRequest(metadata={"businessTopic": "分析", "title_manually_set": False}),
                                      user=SimpleNamespace(user_id="alice"), db=stale)
        assert result["data"]["title"] == "人工标题"
        assert result["data"]["metadata"]["title_manually_set"] is True


def test_real_admission_schedules_title_before_answer_completion(tmp_path, monkeypatch):
    from core.chat.schemas import ChatRequest
    from core.chat.operations import admission, session_context
    from tests.api.test_agent_response_admission import (
        _patch_common_chat_route,
        _route_database,
        _user,
    )
    pass

    engine, factory = _route_database(tmp_path, "admission-title.db")
    monkeypatch.setattr(chat_titles.db_engine, "SessionLocal", factory)
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(session_context, 'load_session_messages', lambda *args: [])
    async def start(**kwargs):
        return kwargs["accepted_run"]
    monkeypatch.setattr(runtime_admission, "start_run", start)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            entered.set()
            await release.wait()
            return "首问标题"
        model(monkeypatch, generate)
        with factory() as db:
            response = await admission.start_response_run(
                ChatRequest(chat_id="chat-1", message="首条问题", stream=True), _user())
        assert response.run_id
        await asyncio.wait_for(entered.wait(), 2)
        with factory() as db:
            row = db.get(ChatSession, "chat-1")
            assert row.extra_data["title_generation"]["status"] == "pending"
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with factory() as db:
            assert db.get(ChatSession, "chat-1").title == "首问标题"
    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_title_write_keeps_concurrent_message_activity_time(env, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import event

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            entered.set()
            await release.wait()
            return "新标题"
        model(monkeypatch, generate)
        task = chat_titles.schedule_first_title("chat", "alice", "run")
        await asyncio.wait_for(entered.wait(), 2)
        engine = env.kw["bind"]
        target = datetime(2030, 1, 1, tzinfo=timezone.utc)
        def concurrent_activity(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE CHAT_SESSIONS") and "title=?" in statement:
                # Interleave the message timestamp update after the title reader's
                # SELECT, on the same transaction before its guarded UPDATE.
                cursor.execute("UPDATE chat_sessions SET updated_at=? WHERE chat_id=?", ("2030-01-01 00:00:00.000000", "chat"))
        event.listen(engine, "before_cursor_execute", concurrent_activity)
        try:
            release.set()
            await task
        finally:
            event.remove(engine, "before_cursor_execute", concurrent_activity)
        with env() as db:
            row = db.get(ChatSession, "chat")
            assert row.updated_at.replace(tzinfo=timezone.utc) == target
    asyncio.run(scenario())
