"""Mid-run chat steering: Redis hand-off and ReAct middleware behavior."""

from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from agentscope.message import ToolResultState
from agentscope.tool._response import ToolChunk
from core.llm.middlewares import SteerMiddleware
from core.services import chat_steer_service
from orchestration import chat_run_executor as executor


@pytest.mark.asyncio
async def test_pending_steer_round_trip_is_single_consumer(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: redis)

    payload = {"steer_id": "s1", "message": "换一种实现", "run_id": "r1"}
    await chat_steer_service.put_pending_steer("r1", payload)

    assert await chat_steer_service.take_pending_steer("r1") == payload
    assert await chat_steer_service.take_pending_steer("r1") is None


@pytest.mark.asyncio
async def test_withdraw_only_removes_matching_steer(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: redis)
    await chat_steer_service.put_pending_steer(
        "r1",
        {"steer_id": "newer", "message": "保留这条"},
    )

    assert await chat_steer_service.remove_pending_steer("r1", "older") is False
    assert (await chat_steer_service.take_pending_steer("r1"))["steer_id"] == "newer"


@pytest.mark.asyncio
async def test_middleware_interrupts_tool_then_injects_user_instruction(monkeypatch):
    delivery = {"steer_id": "s1", "message": "先停下来，改查第二个方案"}

    async def take_pending_steer(run_id: str):
        assert run_id == "r1"
        return delivery

    monkeypatch.setattr(chat_steer_service, "take_pending_steer", take_pending_steer)
    middleware = SteerMiddleware()
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))
    tool_executed = False

    async def tool_handler(**_kwargs):
        nonlocal tool_executed
        tool_executed = True
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    results = [item async for item in middleware.on_acting(agent, {}, tool_handler)]

    assert tool_executed is False
    assert results[-1].state == ToolResultState.INTERRUPTED

    async def model_handler(**_kwargs):
        yield "model-started"

    model_events = [item async for item in middleware.on_reasoning(agent, {}, model_handler)]

    assert model_events == ["model-started"]
    assert agent.state.steer_delivery == delivery
    assert agent.state.context[-1].role == "user"
    assert "改查第二个方案" in agent.state.context[-1].content[0].text


@pytest.mark.asyncio
async def test_middleware_injects_steer_received_while_tool_was_running(monkeypatch):
    delivery = {"steer_id": "s2", "message": "工具完成后，改做新的问题"}
    polls = 0

    async def take_pending_steer(run_id: str):
        nonlocal polls
        assert run_id == "r1"
        polls += 1
        # No instruction existed when the tool started. It arrived while that
        # tool was running and must be picked up before the next model call.
        return None if polls == 1 else delivery

    monkeypatch.setattr(chat_steer_service, "take_pending_steer", take_pending_steer)
    middleware = SteerMiddleware()
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))
    tool_executed = False

    async def tool_handler(**_kwargs):
        nonlocal tool_executed
        tool_executed = True
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    results = [item async for item in middleware.on_acting(agent, {}, tool_handler)]
    assert tool_executed is True
    assert results[-1].state == ToolResultState.SUCCESS

    async def model_handler(**_kwargs):
        yield "next-model-started"

    model_events = [item async for item in middleware.on_reasoning(agent, {}, model_handler)]

    assert polls == 2
    assert model_events == ["next-model-started"]
    assert agent.state.steer_delivery == delivery
    assert agent.state.context[-1].role == "user"
    assert "工具完成后，改做新的问题" in agent.state.context[-1].content[0].text
    assert "上一轮工具结果已经完成" in agent.state.context[-1].content[0].text


@pytest.mark.asyncio
async def test_middleware_is_pass_through_without_pending_steer(monkeypatch):
    async def take_pending_steer(_run_id: str):
        return None

    monkeypatch.setattr(chat_steer_service, "take_pending_steer", take_pending_steer)
    middleware = SteerMiddleware()
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))

    async def tool_handler(**_kwargs):
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    results = [item async for item in middleware.on_acting(agent, {}, tool_handler)]
    assert results[-1].state == ToolResultState.SUCCESS


@pytest.mark.asyncio
async def test_executor_persists_steer_at_the_stream_boundary(monkeypatch):
    persisted = []
    emitted = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeChatService:
        def __init__(self, _db):
            pass

        def add_message(self, **kwargs):
            persisted.append((kwargs["role"], kwargs["content"], kwargs["message_id"]))

        def upsert_message(self, **kwargs):
            persisted.append((kwargs["role"], kwargs["content"], kwargs["message_id"]))

    async def fake_workflow(**_kwargs):
        yield {"type": "content", "delta": "前半段"}
        yield {"type": "steer_applied", "steer_id": "s1", "message": "改用第二种方案"}
        yield {"type": "content", "delta": "后半段"}
        yield {"type": "meta", "is_markdown": False, "usage": {}}

    async def fake_xadd(_run_id, _offset, event):
        emitted.append(dict(event))

    monkeypatch.setattr(executor, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(executor, "ChatService", FakeChatService)
    monkeypatch.setattr(executor, "astream_chat_workflow", fake_workflow)
    monkeypatch.setattr(executor, "_xadd_event", fake_xadd)
    monkeypatch.setattr(executor, "_update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "_finalize_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)

    async def fake_expire(_run_id):
        return None

    monkeypatch.setattr(executor, "_expire_stream", fake_expire)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    context = {"chat_id": "c1", "user_id": "u1"}
    await executor._run_workflow(
        run_id="r1",
        chat_id="c1",
        user_id="u1",
        message_id="m1",
        session_messages=[],
        effective_user_message="原始问题",
        raw_user_message="原始问题",
        context=context,
        model_name="test-model",
    )

    assert [(role, content) for role, content, _ in persisted] == [
        ("assistant", "前半段"),
        ("user", "改用第二种方案"),
        ("assistant", "后半段"),
    ]
    steer_event = next(event for event in emitted if event.get("type") == "steer_applied")
    assert steer_event["previous_assistant_message_id"] == "m1"
    assert persisted[-1][2] == steer_event["next_assistant_message_id"]
    assert context["message_id"] == steer_event["next_assistant_message_id"]
