"""助手消息那一行从轮次接纳起就存在、跑的过程中增量刷新、结束时定稿。

这是「整轮回复从页面上消失」的根治：服务端任何时刻都持有这一轮的展示状态，前端不再是唯一副本。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from core.chat import inflight
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.services.chat_sequencer import ChatSequencer
from core.services.compaction_service import _normalize_rows
from core.services.run_journal import RunJournal
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'inflight.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


def _assistant_rows(sessions):
    with sessions() as db:
        return (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == "chat-1", ChatMessage.role == "assistant")
            .order_by(ChatMessage.chat_seq)
            .all()
        )


async def _start(workflow, monkeypatch, sessions):
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    with sessions() as db:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="hello",
            request_payload={"message": "hello"},
        )
    return await executor.start_run(
        accepted_run=accepted.run,
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )


async def _wait_for(predicate, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _finish(worker):
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(worker, timeout=5)


def test_admission_creates_inflight_assistant_row(env):
    sessions = env
    with sessions() as db:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="hello",
            request_payload={"message": "hello"},
        )
        run = accepted.run
        row = db.get(ChatMessage, run.message_id)
        assert row is not None and row.role == "assistant"
        assert row.chat_seq == run.assistant_chat_seq
        assert row.content == ""
        assert inflight.marker(row.extra_data)["run_id"] == run.run_id
        # 两行（用户 + 助手）一次记清，之后的定稿走 update 路径不再加。
        assert db.get(ChatSession, "chat-1").message_count == 2


@pytest.mark.asyncio
async def test_streaming_refreshes_the_row_before_the_turn_ends(env, monkeypatch):
    sessions = env
    reached = asyncio.Event()

    async def workflow(**_kwargs):
        yield {"type": "ai_message", "delta": "第一段正文"}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-1",
            "tool_args": {"command": "sleep 100"},
        }
        reached.set()
        await asyncio.Event().wait()

    run = await _start(workflow, monkeypatch, sessions)
    await asyncio.wait_for(reached.wait(), timeout=2)

    def _persisted():
        rows = _assistant_rows(sessions)
        return rows[0] if rows and rows[0].tool_calls else None

    row = await _wait_for(_persisted)
    assert row.message_id == run.message_id
    assert "第一段正文" in row.content
    assert row.tool_calls[0]["tool_id"] == "call-1"
    marker = row.extra_data[inflight.IN_FLIGHT_KEY]
    assert marker["run_id"] == run.run_id
    assert marker["event_offset"] > 0
    assert row.extra_data["segments"]

    executor._active_runs[run.run_id].cancel()
    await _finish(executor._active_runs[run.run_id])


@pytest.mark.asyncio
async def test_completion_finalizes_and_clears_marker(env, monkeypatch):
    sessions = env

    async def workflow(**_kwargs):
        yield {"type": "ai_message", "delta": "完整回答"}
        yield {"type": "meta", "route": "main", "is_markdown": False, "usage": {}}

    run = await _start(workflow, monkeypatch, sessions)
    await _finish(executor._active_runs[run.run_id])

    rows = _assistant_rows(sessions)
    assert len(rows) == 1
    assert rows[0].content == "完整回答"
    assert inflight.marker(rows[0].extra_data) is None
    with sessions() as db:
        assert db.get(ChatRun, run.run_id).status == "completed"
        assert db.get(ChatSession, "chat-1").message_count == 2


@pytest.mark.asyncio
async def test_failure_keeps_what_was_produced(env, monkeypatch):
    """现场记录第一次事故的回归：轮次异常失败时已产出的正文和工具卡不再被清空。"""
    sessions = env

    async def workflow(**_kwargs):
        yield {"type": "ai_message", "delta": "跑到一半"}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-1",
            "tool_args": {"command": "ls"},
        }
        raise RuntimeError("sandbox vanished")

    run = await _start(workflow, monkeypatch, sessions)
    await _finish(executor._active_runs[run.run_id])

    rows = _assistant_rows(sessions)
    assert len(rows) == 1
    assert "跑到一半" in rows[0].content
    assert rows[0].tool_calls and rows[0].tool_calls[0]["tool_id"] == "call-1"
    assert rows[0].error and "sandbox vanished" in rows[0].error["error"]
    assert inflight.marker(rows[0].extra_data) is None
    with sessions() as db:
        assert db.get(ChatRun, run.run_id).status == "failed"


def test_inflight_rows_never_reach_model_context():
    """只有**活着的** run 正在写的行被跳过；死 run 留下的标记不再有意义，行按定稿读。"""
    rows = [
        SimpleNamespace(role="user", content="q1", extra_data={}, tool_calls=None),
        SimpleNamespace(
            role="assistant",
            content="half",
            extra_data={inflight.IN_FLIGHT_KEY: inflight.mark("run-live", "streaming")},
            tool_calls=None,
        ),
        SimpleNamespace(
            role="assistant",
            content="left by a dead run",
            extra_data={inflight.IN_FLIGHT_KEY: inflight.mark("run-dead", "streaming")},
            tool_calls=None,
        ),
        SimpleNamespace(role="assistant", content="final", extra_data={}, tool_calls=None),
    ]
    out = _normalize_rows(rows, frozenset({"run-live"}))
    assert [m["content"] for m in out] == ["q1", "left by a dead run", "final"]


def test_fenced_worker_cannot_refresh_the_row(env):
    sessions = env
    journal = RunJournal(sessions)
    with sessions() as db:
        run = (
            ChatSequencer(db)
            .accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="hello",
                request_payload={"message": "hello"},
            )
            .run
        )
    assert journal.claim(run.run_id, owner="worker-a", lease_seconds=60) is not None
    from core.services.chat_service import ChatService

    def _refresh(owner: str) -> bool:
        with sessions() as db:
            ok = ChatService(db).refresh_streaming_message(
                run_id=run.run_id,
                owner=owner,
                message_id=run.message_id,
                content=f"written by {owner}",
                thinking=None,
                tool_calls=None,
                extra_data={inflight.IN_FLIGHT_KEY: inflight.mark(run.run_id, "streaming", 1)},
            )
            db.commit()
            return ok

    assert _refresh("worker-b") is False
    assert _refresh("worker-a") is True
    with sessions() as db:
        assert db.get(ChatMessage, run.message_id).content == "written by worker-a"


def test_a_row_reads_as_final_once_its_run_is_gone(env):
    """in-flight 不是存下来的状态，而是"归属的 run 是否还活着"：run 一停，行就是定稿。"""
    from core.services.chat_service import ChatService
    from core.services.compaction_service import _live_run_ids

    sessions = env
    journal = RunJournal(sessions)
    with sessions() as db:
        run = (
            ChatSequencer(db)
            .accept_main_run(
                chat_id="chat-1",
                user_id="user-1",
                user_content="hello",
                request_payload={"message": "hello"},
            )
            .run
        )
        rows = db.query(ChatMessage).filter(ChatMessage.chat_id == "chat-1").all()
        assert _live_run_ids(ChatService(db), rows) == frozenset({run.run_id})
    assert journal.cancel(run.run_id, reason="user stop") is True
    with sessions() as db:
        rows = db.query(ChatMessage).filter(ChatMessage.chat_id == "chat-1").all()
        assert _live_run_ids(ChatService(db), rows) == frozenset()
