"""思考过程与正文分列存储。

生产事故：deepseek 系模型的 reasoning 走独立通道，后端却把它包成 <think>…</think>
拼进正文一起落库。「迟到思考并回前一块」的规则没有轮次边界，正文一出现就永远成立，
于是每一轮的思考都被塞进同一个块无限膨胀，把正文挤出 content 的 10 万字上限；截断
又切掉了闭合标签，刷新后整段思考被当正文渲染到页面上。

现在思考落进 chat_messages.thinking 独立一列，每块带 offset（出现时的正文字符
位置），刷新后由前端按该位置原样插回——存储分开，展示位置不变。
"""

from __future__ import annotations

import asyncio
import contextlib

import fakeredis.aioredis
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatSession
from core.services.chat_service import _CONTENT_MAX, _THINKING_MAX, clamp_thinking
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'executor-think.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "get_redis", lambda **_: redis)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


async def _run_to_completion(workflow, monkeypatch) -> str:
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
    worker = executor._active_runs.get(run.run_id)
    if worker is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=10)
    return run.message_id


def _stored(sessions, message_id: str) -> ChatMessage:
    with sessions() as db:
        return db.get(ChatMessage, message_id)


def _shape(stored: ChatMessage) -> list:
    """把落库的段落表翻译成一串可读的展示顺序。"""
    out = []
    for seg in (stored.extra_data or {}).get("segments") or []:
        if seg["type"] == "text":
            out.append(("text", seg["text"]))
        elif seg["type"] == "thinking":
            out.append(("think", stored.thinking[seg["index"]]["content"]))
        else:
            out.append(("tool", stored.tool_calls[seg["index"]]["tool_id"]))
    return out


@pytest.mark.asyncio
async def test_reasoning_is_stored_out_of_the_body(run_env, monkeypatch):
    """多轮「思考 → 工具 → 正文」：正文一个思考标记都不许有，思考各自成块。"""

    async def multi_round_workflow(**_kwargs):
        for i in range(4):
            yield {"type": "thinking", "delta": f"第{i}轮在想"}
            yield {
                "type": "tool_call",
                "tool_name": "bash",
                "tool_id": f"call-{i}",
                "tool_args": {"command": "ls"},
            }
            yield {"type": "tool_result", "tool_id": f"call-{i}", "content": "ok"}
            yield {"type": "ai_message", "delta": f"第{i}轮的正文。"}
        yield {"type": "meta"}

    message_id = await _run_to_completion(multi_round_workflow, monkeypatch)
    stored = _stored(run_env, message_id)

    assert "<think>" not in stored.content and "</think>" not in stored.content
    assert stored.content == "".join(f"第{i}轮的正文。" for i in range(4))
    assert [b["content"] for b in stored.thinking] == [f"第{i}轮在想" for i in range(4)]


@pytest.mark.asyncio
async def test_display_order_is_recorded_as_it_streams(run_env, monkeypatch):
    """展示顺序在产生的那一刻就记下来，刷新后照着渲染，不做任何反推。"""

    async def multi_round_workflow(**_kwargs):
        for i in range(3):
            yield {"type": "thinking", "delta": f"想{i}"}
            yield {
                "type": "tool_call",
                "tool_name": "bash",
                "tool_id": f"call-{i}",
                "tool_args": {"command": "ls"},
            }
            yield {"type": "tool_result", "tool_id": f"call-{i}", "content": "ok"}
            yield {"type": "ai_message", "delta": "正文"}
        yield {"type": "meta"}

    message_id = await _run_to_completion(multi_round_workflow, monkeypatch)
    stored = _stored(run_env, message_id)

    assert _shape(stored) == [
        ("think", "想0"), ("tool", "call-0"), ("text", "正文"),
        ("think", "想1"), ("tool", "call-1"), ("text", "正文"),
        ("think", "想2"), ("tool", "call-2"), ("text", "正文"),
    ]


@pytest.mark.asyncio
async def test_steps_between_two_bodies_keep_their_true_order(run_env, monkeypatch):
    """两次可见正文之间发生的一串步骤，先后必须原样落库。

    这是过去反推顺序失败的地方：靠正文字符偏移排序时，这些步骤全落在同一个偏移上，
    刷新后被还原成「想想→跑跑」，而不是真实的「想→跑→想→跑」。
    """

    async def interleaved_workflow(**_kwargs):
        yield {"type": "thinking", "delta": "先想想"}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-0",
            "tool_args": {"command": "ls"},
        }
        yield {"type": "tool_result", "tool_id": "call-0", "content": "ok"}
        yield {"type": "thinking", "delta": "再想想"}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-1",
            "tool_args": {"command": "pwd"},
        }
        yield {"type": "tool_result", "tool_id": "call-1", "content": "ok"}
        yield {"type": "ai_message", "delta": "答案"}
        yield {"type": "meta"}

    message_id = await _run_to_completion(interleaved_workflow, monkeypatch)
    stored = _stored(run_env, message_id)

    assert _shape(stored) == [
        ("think", "先想想"),
        ("tool", "call-0"),
        ("think", "再想想"),
        ("tool", "call-1"),
        ("text", "答案"),
    ]


@pytest.mark.asyncio
async def test_late_reasoning_tail_merges_into_the_same_round_block(run_env, monkeypatch):
    """同一轮里正文开头之后才到的思考尾巴，并回本轮的块，不另起一块、不改位置。"""

    async def late_tail_workflow(**_kwargs):
        yield {"type": "thinking", "delta": "想好了"}
        yield {"type": "ai_message", "delta": "答案是"}
        yield {"type": "thinking", "delta": "。"}
        yield {"type": "ai_message", "delta": "42"}
        yield {"type": "meta"}

    message_id = await _run_to_completion(late_tail_workflow, monkeypatch)
    stored = _stored(run_env, message_id)

    assert stored.content == "答案是42"
    assert stored.thinking == [{"content": "想好了。"}]
    assert _shape(stored) == [("think", "想好了。"), ("text", "答案是42")]


@pytest.mark.asyncio
async def test_runaway_reasoning_no_longer_evicts_the_answer(run_env, monkeypatch):
    """事故复现：思考超过 content 上限时，正文必须完好无损。"""

    async def runaway_workflow(**_kwargs):
        yield {"type": "thinking", "delta": "想" * (_CONTENT_MAX + 5000)}
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-0",
            "tool_args": {"command": "ls"},
        }
        yield {"type": "tool_result", "tool_id": "call-0", "content": "ok"}
        yield {"type": "ai_message", "delta": "最终答案"}
        yield {"type": "meta"}

    message_id = await _run_to_completion(runaway_workflow, monkeypatch)
    stored = _stored(run_env, message_id)

    assert stored.content == "最终答案"
    assert "<think>" not in stored.content


def test_thinking_has_its_own_budget_and_keeps_the_latest_rounds():
    blocks = [{"content": "旧" * _THINKING_MAX}, {"content": "新"}]

    kept, _ = clamp_thinking(blocks)

    assert sum(len(b["content"]) for b in kept) <= _THINKING_MAX
    # 最后一轮离最终答案最近，必须留下；被裁的是最早那块。
    assert kept[-1] == {"content": "新"}


def test_clamping_thinking_keeps_the_segment_indices_pointing_at_the_right_block():
    """段落表按下标引用思考块，丢块必须同步改下标，否则展示会错位。"""

    # 最后一块就吃满额度 → 更早的那块整块被丢。
    blocks = [{"content": "旧"}, {"content": "新" * _THINKING_MAX}]
    segments = [
        {"type": "thinking", "index": 0},
        {"type": "text", "text": "答案"},
        {"type": "thinking", "index": 1},
    ]

    kept, kept_segments = clamp_thinking(blocks, segments)

    # 第 0 块被丢 → 引用它的段落一并剔除；第 1 块变成第 0 块，引用要跟着改。
    assert len(kept) == 1 and kept[0]["content"].startswith("新")
    assert kept_segments == [
        {"type": "text", "text": "答案"},
        {"type": "thinking", "index": 0},
    ]


def test_clamp_thinking_passes_small_payloads_through():
    blocks = [{"content": "想"}, {"content": "再想"}]

    assert clamp_thinking(blocks) == (blocks, None)
    assert clamp_thinking([]) == (None, None)
    assert clamp_thinking(None) == (None, None)
