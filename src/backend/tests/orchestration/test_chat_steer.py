"""Mid-run chat steering: Redis hand-off and ReAct middleware behavior."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from agentscope.message import ToolResultState
from agentscope.tool._response import ToolChunk
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, ChatSteerQueueItem
from core.llm.middlewares import SteerMiddleware
from core.services import chat_steer_service
from core.services.run_journal import RunJournal, RunLeaseLost
from core.services.steer_queue import SteerQueue
from orchestration import chat_run_executor as executor


@pytest.fixture()
def durable_steer_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat-steer.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    RunJournal(sessions).accept(
        run_id="r1",
        message_id="m1",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    monkeypatch.setattr(chat_steer_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_pending_steer_round_trip_is_single_consumer(
    durable_steer_env, monkeypatch
):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: redis)

    payload = {"steer_id": "s1", "message": "换一种实现", "run_id": "r1"}
    await chat_steer_service.put_pending_steer("r1", payload)

    claimed = await chat_steer_service.take_pending_steer("r1", owner="worker-1")
    assert claimed["steer_id"] == "s1"
    assert claimed["message"] == "换一种实现"
    assert claimed["status"] == "claimed"
    assert claimed["claim_owner"] == "worker-1"
    assert await chat_steer_service.take_pending_steer("r1") is None


@pytest.mark.asyncio
async def test_withdraw_only_removes_matching_steer(durable_steer_env, monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: redis)
    await chat_steer_service.put_pending_steer(
        "r1",
        {"steer_id": "newer", "message": "保留这条"},
    )

    assert await chat_steer_service.remove_pending_steer("r1", "older") is False
    assert (await chat_steer_service.take_pending_steer("r1"))["steer_id"] == "newer"


@pytest.mark.asyncio
async def test_redis_loss_does_not_drop_database_instruction(
    durable_steer_env, monkeypatch
):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: redis)
    await chat_steer_service.put_pending_steer(
        "r1",
        {"steer_id": "db-first", "message": "数据库里的指令"},
    )
    await redis.flushall()

    claimed = await chat_steer_service.take_pending_steer("r1", owner="worker-db")

    assert claimed["steer_id"] == "db-first"
    assert claimed["message"] == "数据库里的指令"
    with durable_steer_env() as db:
        row = db.get(ChatSteerQueueItem, claimed["queue_id"])
        assert row.status == "claimed"


@pytest.mark.asyncio
async def test_redis_notification_failure_happens_after_durable_accept(
    durable_steer_env, monkeypatch
):
    class BrokenRedis:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(chat_steer_service, "get_redis", lambda: BrokenRedis())
    accepted = await chat_steer_service.put_pending_steer(
        "r1",
        {"steer_id": "db-before-redis", "message": "先写数据库"},
    )

    with durable_steer_env() as db:
        row = db.get(ChatSteerQueueItem, accepted["queue_id"])
        assert row.status == "accepted"
        assert row.message == "先写数据库"


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

        def get(self, *_args):
            return SimpleNamespace(
                operation_seq=1,
                snapshot_version=1,
                recovery_snapshot=None,
            )

    class FakeChatService:
        def __init__(self, _db):
            pass

        def add_message(self, **kwargs):
            persisted.append((kwargs["role"], kwargs["content"], kwargs["message_id"]))

        def upsert_message(self, **kwargs):
            persisted.append((kwargs["role"], kwargs["content"], kwargs["message_id"]))

    class FakeJournal:
        def __init__(self):
            self.offset = 0

        def claim(self, *_args, **_kwargs):
            return True

        def renew(self, *_args, **_kwargs):
            return True

        def append_operation(self, *_args, **kwargs):
            effect = kwargs.get("commit_effect")
            if effect is not None:
                effect(FakeSession())
            return None

        def save_snapshot(self, *_args, **_kwargs):
            return None

        def complete(self, *_args, **kwargs):
            effect = kwargs.get("commit_effect")
            if effect is not None:
                effect(FakeSession())
            return True

        def allocate_event_offset(self, *_args, **_kwargs):
            self.offset += 1
            return self.offset

    async def fake_workflow(**_kwargs):
        yield {"type": "content", "delta": "前半段"}
        yield {"type": "steer_applied", "steer_id": "s1", "message": "改用第二种方案"}
        yield {"type": "content", "delta": "后半段"}
        yield {"type": "meta", "is_markdown": False, "usage": {}}

    async def fake_xadd(_run_id, _offset, event):
        emitted.append(dict(event))

    monkeypatch.setattr(executor, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(executor, "ChatService", FakeChatService)
    fake_journal = FakeJournal()
    monkeypatch.setattr(executor, "_journal", lambda: fake_journal)
    monkeypatch.setattr(executor, "astream_chat_workflow", fake_workflow)
    monkeypatch.setattr(executor, "_xadd_event", fake_xadd)
    monkeypatch.setattr(executor, "_update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "_claim_run_execution", lambda _run_id: True)
    monkeypatch.setattr(executor, "_acknowledge_terminal_writer", lambda _run_id: False)
    monkeypatch.setattr(executor, "_finalize_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        executor,
        "_commit_queued_handoff_in_session",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(executor, "is_run_cancelled", lambda _run_id: False)
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
        assistant_chat_seq=1,
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


@pytest.mark.asyncio
async def test_executor_atomically_applies_claimed_queue_item(
    durable_steer_env, monkeypatch
):
    sessions = durable_steer_env
    queue = SteerQueue(sessions)
    accepted = queue.accept(
        target_run_id="r1",
        chat_id="chat-1",
        user_id="user-1",
        steer_id="durable-steer",
        message="改用数据库方案",
        replace_latest=False,
    )
    claimed = queue.claim_next("r1", owner="delivery-owner", lease_seconds=90)
    assert claimed is not None

    async def fake_workflow(**_kwargs):
        yield {"type": "content", "delta": "旧方案"}
        yield {
            "type": "steer_applied",
            **claimed.as_payload(),
        }
        yield {"type": "content", "delta": "新方案"}
        yield {"type": "meta", "is_markdown": False, "usage": {}}

    async def no_event_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(executor, "SessionLocal", sessions)
    monkeypatch.setattr(executor, "astream_chat_workflow", fake_workflow)
    monkeypatch.setattr(executor, "_xadd_event", no_event_write)
    monkeypatch.setattr(executor, "_expire_stream", no_event_write)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    await executor._run_workflow(
        run_id="r1",
        chat_id="chat-1",
        user_id="user-1",
        message_id="m1",
        session_messages=[{"role": "user", "content": "原问题"}],
        effective_user_message="原问题",
        raw_user_message="原问题",
        context={"chat_id": "chat-1", "user_id": "user-1"},
        model_name="test-model",
        journal_owner="run-owner",
    )

    with sessions() as db:
        applied = db.get(ChatSteerQueueItem, accepted.queue_id)
        run = db.get(ChatRun, "r1")
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == "chat-1")
            .order_by(ChatMessage.created_at, ChatMessage.message_id)
            .all()
        )
        assert applied.status == "applied"
        assert applied.applied_run_id == "r1"
        assert applied.applied_operation_seq is not None
        assert run.status == "completed"
        assert [(row.role, row.content) for row in messages] == [
            ("assistant", "旧方案"),
            ("user", "改用数据库方案"),
            ("assistant", "新方案"),
        ]


@pytest.mark.asyncio
async def test_crash_after_steer_ack_safe_stops_with_exact_post_steer_context(
    durable_steer_env, monkeypatch
):
    sessions = durable_steer_env
    queue = SteerQueue(sessions)
    accepted = queue.accept(
        target_run_id="r1",
        chat_id="chat-1",
        user_id="user-1",
        steer_id="crash-steer",
        message="崩溃后也要继续",
        replace_latest=False,
    )
    claimed = queue.claim_next("r1", owner="delivery-owner", lease_seconds=90)
    assert claimed is not None

    async def crashing_workflow(**_kwargs):
        yield {"type": "content", "delta": "崩溃前回答"}
        yield {"type": "steer_applied", **claimed.as_payload()}
        raise RunLeaseLost("simulated process loss after durable steer commit")

    async def no_event_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(executor, "SessionLocal", sessions)
    monkeypatch.setattr(executor, "astream_chat_workflow", crashing_workflow)
    monkeypatch.setattr(executor, "_xadd_event", no_event_write)
    monkeypatch.setattr(executor, "_expire_stream", no_event_write)

    await executor._run_workflow(
        run_id="r1",
        chat_id="chat-1",
        user_id="user-1",
        message_id="m1",
        session_messages=[{"role": "user", "content": "原问题"}],
        effective_user_message="原问题",
        raw_user_message="原问题",
        context={"chat_id": "chat-1", "user_id": "user-1"},
        model_name="test-model",
        journal_owner="run-owner",
    )

    with sessions() as db:
        run = db.get(ChatRun, "r1")
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    decisions = RunJournal(sessions).recover()

    assert len(decisions) == 1
    # The acknowledgement is observed with the next provider call already in
    # flight, so replay is unsafe. We safe-stop, but retain the exact context
    # needed for an explicit/manual continuation.
    assert decisions[0].action == "needs_attention"
    worker_args = decisions[0].snapshot["worker_args"]
    assert worker_args["effective_user_message"] == "崩溃后也要继续"
    assert worker_args["context"]["message_id"] != "m1"
    assert worker_args["session_messages"] == [
        {"role": "user", "content": "原问题"},
        {"role": "assistant", "content": "崩溃前回答"},
        {"role": "user", "content": "崩溃后也要继续"},
    ]
    with sessions() as db:
        assert db.get(ChatSteerQueueItem, accepted.queue_id).status == "applied"
