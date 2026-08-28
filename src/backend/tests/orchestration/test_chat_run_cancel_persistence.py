"""用户取消一轮对话后，已产出的半截回答必须留在库里（刷新页面还看得到）。"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def cancel_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'executor-cancel.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda: redis)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


async def _start_and_cancel(workflow, monkeypatch, ready: asyncio.Event):
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )
    await asyncio.wait_for(ready.wait(), timeout=2)
    worker = executor._active_runs[run.run_id]
    assert await executor.cancel_run(run.run_id, user_id="user-1") is True
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(worker, timeout=2)
    return run


@pytest.mark.asyncio
async def test_user_cancel_persists_partial_answer(cancel_env, monkeypatch):
    sessions = cancel_env
    streamed = asyncio.Event()

    async def half_written_workflow(**_kwargs):
        yield {"type": "ai_message", "delta": "已经写了一半"}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-1",
            "tool_args": {"command": "sleep 100"},
        }
        yield {"type": "thinking", "delta": "再想想"}
        streamed.set()
        await asyncio.Event().wait()

    run = await _start_and_cancel(half_written_workflow, monkeypatch, streamed)

    with sessions() as db:
        assert db.get(ChatRun, run.run_id).status == "cancelled"
        stored = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == "chat-1", ChatMessage.role == "assistant")
            .all()
        )
    assert len(stored) == 1
    assert stored[0].message_id == run.message_id
    assert "已经写了一半" in stored[0].content
    assert "<think>再想想</think>" in stored[0].content
    assert stored[0].extra_data["cancelled"] is True
    # 没跑完的工具卡片要落成"已中断"，否则刷新后会渲染成执行成功
    assert [(tc["tool_name"], tc["status"]) for tc in stored[0].tool_calls] == [
        ("bash", "interrupted")
    ]


@pytest.mark.asyncio
async def test_user_cancel_before_any_output_persists_nothing(cancel_env, monkeypatch):
    sessions = cancel_env
    entered = asyncio.Event()

    async def silent_workflow(**_kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield {"type": "meta"}

    await _start_and_cancel(silent_workflow, monkeypatch, entered)

    with sessions() as db:
        assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0


def test_cancelled_turn_is_marked_in_replayed_context():
    """继续会话时，被停掉那轮要带上"被中断"的标注，否则模型以为自己已经说完了。"""
    from core.services.compaction_service import _normalize_rows

    rows = [
        SimpleNamespace(role="user", content="帮我查一下", extra_data={}, tool_calls=None),
        SimpleNamespace(
            role="assistant",
            content="我先看看",
            extra_data={"cancelled": True},
            tool_calls=None,
        ),
    ]

    replayed = _normalize_rows(rows)

    assert replayed[0] == {"role": "user", "content": "帮我查一下"}
    assert replayed[1]["role"] == "assistant"
    assert replayed[1]["content"] == "我先看看\n\n[本轮回答被用户中断]"


def test_normal_turn_carries_no_interruption_marker():
    from core.services.compaction_service import _normalize_rows

    rows = [SimpleNamespace(role="assistant", content="查完了", extra_data={}, tool_calls=None)]

    assert _normalize_rows(rows) == [{"role": "assistant", "content": "查完了"}]
