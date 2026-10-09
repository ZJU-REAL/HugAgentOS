"""Public resume restores the event log's aligned snapshot after trimming."""
import asyncio
import json

import fakeredis.aioredis
import pytest

from api.routes.v1.chats import run_views
from core.auth.backend import UserContext
from core.services.run_journal import RunJournal
from orchestration import run_event_stream
from tests.orchestration.recovery_test_support import recovery_env


@pytest.mark.parametrize("mode", ["running", "completed", "two_writers", "steered", "controls", "phase", "replacement", "resolved_pick", "resolved_confirm", "checkpoint_tail"])
@pytest.mark.asyncio
async def test_resume_restores_message_and_child_steps_after_prefix_is_trimmed(recovery_env, monkeypatch, mode):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(run_event_stream, "get_redis", lambda **_: redis)
    monkeypatch.setattr(run_event_stream, "MAXLEN", 8 if mode == "checkpoint_tail" else 2)
    await run_event_stream.get_run_event_stream().clear("run-trimmed")
    journal = RunJournal(recovery_env)
    journal.accept(run_id="run-trimmed", message_id="msg-trimmed", chat_id="chat-1",
                   user_id="user-1", request_payload={}, recovery_snapshot={})
    assert journal.claim("run-trimmed", owner="worker", lease_seconds=60)
    stream = run_event_stream.get_run_event_stream()
    for offset, event in enumerate([
        {"type": "run_started", "message_id": "msg-trimmed", "run_id": "run-trimmed", "started_at": 1000},
        {"type": "thinking", "structured_reasoning": True, "delta": "准备下一批"},
        {"type": "content", "delta": "前 11 个批次完成。"},
        {"type": "tool_call", "tool_id": "child-26", "tool_name": "call_subagent"},
        {"type": "subagent_event", "parent_tool_id": "child-26", "sub_type": "content", "delta": "继续核对"},
    ], 1):
        await stream.append("run-trimmed", {**event, "_offset": offset, "server_ts": 1000 + offset})
    await redis.xtrim(run_event_stream.redis_stream_key("run-trimmed"), maxlen=1, approximate=False)
    if mode == "phase":
        await stream.append("run-trimmed", {"type": "tool_result", "tool_id": "child-26",
            "tool_name": "call_subagent", "result": "完成", "_offset": 6})
        await stream.append("run-trimmed", {"type": "plan_update", "steps": [], "_offset": 7})
    if mode == "replacement":
        await stream.append("run-trimmed", {"type": "content_replace", "content": "修订答案。", "_offset": 6})
    if mode == "resolved_confirm":
        from orchestration import run_subscription
        async def absent_confirmation(*_):
            return None
        monkeypatch.setattr(run_subscription.interaction_store, "read", absent_confirmation)
        await stream.append("run-trimmed", {"type": "file_confirm", "confirm_id": "decided", "_offset": 6})
    if mode == "resolved_pick":
        await stream.append("run-trimmed", {"type": "design_pick", "confirm_id": "pick-1", "_offset": 6})
        await stream.append("run-trimmed", {"type": "tool_result", "tool_id": "design",
            "tool_name": "choose_design", "result": "已选择", "_offset": 7})
    if mode == "controls":
        await stream.append("run-trimmed", {"type": "user_question", "request_id": "question-1",
            "questions": [{"id": "q1", "question": "继续吗？"}], "_offset": 6})
    if mode == "two_writers":
        other = run_event_stream.RedisRunEventStream()
        await other.append("run-trimmed", {"type": "content", "delta": "后续进展。", "_offset": 6})
        await stream.append("run-trimmed", {"type": "content", "delta": "全部核对。", "_offset": 7})
        await redis.xtrim(run_event_stream.redis_stream_key("run-trimmed"), maxlen=1, approximate=False)
    elif mode == "completed":
        await stream.append("run-trimmed", {"type": "content", "delta": "最终答案。", "_offset": 6})
        assert journal.complete("run-trimmed", owner="worker", status="completed", last_event_offset=7)
    elif mode == "steered":
        await stream.append("run-trimmed", {
            "type": "steer_applied", "message_id": "user-steer", "next_assistant_message_id": "next-answer",
            "message": "改为三十家", "had_assistant_output": True, "_offset": 6,
        })
        await stream.append("run-trimmed", {"type": "thinking", "structured_reasoning": True, "_offset": 7})
        await stream.append("run-trimmed", {"type": "content", "delta": "已调整。", "_offset": 8})
    with recovery_env() as db:
        response = run_views.chat_run_subscription(
            "run-trimmed",
            user=UserContext(user_id="user-1", user_center_id="center-1", username="tester"), db=db,
        )
    iterator = response.body_iterator
    try:
        async with asyncio.timeout(2):
            frame = await anext(iterator)
    finally:
        await iterator.aclose()
    snapshot = json.loads(frame[6:])
    assert snapshot["type"] == "run_snapshot"
    state = snapshot["state"]
    if mode == "resolved_confirm":
        assert not any(v["type"] == "file_confirm" for v in state["signals"].values())
        return
    if mode == "phase":
        assert state["blocks"][-2:] == [{"kind": "phase"}, {"kind": "phase"}]
        return
    if mode == "replacement":
        assert state["blocks"][-1] == {"kind": "answer", "content": "修订答案。"}
        assert state["blocks"][0]["kind"] == "protocol"
        assert state["structured_reasoning"] is True
        return
    if mode == "resolved_pick":
        assert not any(v["type"] == "design_pick" for v in state["signals"].values())
        return
    if mode == "steered":
        assert snapshot["event_offset"] == 8
        assert state["message_id"] == "next-answer"
        assert state["blocks"] == [{"kind": "protocol", "structured": True}, {"kind": "text", "content": "已调整。"}]
        assert state["history"][0]["state"]["message_id"] == "msg-trimmed"
        assert state["history"][1]["content"] == "改为三十家"
        return
    if mode == "controls":
        assert snapshot["event_offset"] == 6
        assert state["signals"]["user_question::question-1"]["questions"][0]["question"] == "继续吗？"
        return
    assert snapshot["event_offset"] == {"running": 5, "checkpoint_tail": 5, "completed": 6, "two_writers": 7}[mode]
    assert state["message_id"] == "msg-trimmed"
    if mode == "completed":
        assert state["terminal"] is True
        assert state["blocks"][-1] == {"kind": "text", "content": "最终答案。"}
    elif mode == "two_writers":
        assert state["blocks"][-1] == {"kind": "text", "content": "后续进展。全部核对。"}
    if mode not in {"running", "checkpoint_tail"}:
        assert state["tools"][0]["subSteps"][0]["text"] == "继续核对"
        return
    assert state["blocks"] == [
        {"kind": "protocol", "structured": True},
        {"kind": "thinking", "content": "准备下一批", "structured": True},
        {"kind": "text", "content": "前 11 个批次完成。"},
        {"kind": "tool", "index": 0},
    ]
    assert state["tools"][0]["subSteps"][0]["text"] == "继续核对"
    assert state["tools"][0]["status"] == "running"
