"""Behavioral regression coverage."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import api.routes.v1.chats.run_views as chat_run_views
import fakeredis.aioredis
import pytest
from core.auth.backend import UserContext
from core.db.models import ChatMessage, ChatRun
from core.services.run_journal import RunJournal
from orchestration import chat_run_executor as executor
from orchestration import run_event_stream
from tests.orchestration.recovery_test_support import recovery_env


@pytest.mark.asyncio
async def test_public_start_follow_and_history_complete_on_durable_offsets(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "public answer"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": False,
            "usage": {"input_tokens": 2, "output_tokens": 2},
        }

    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

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

    async def collect():
        return [event async for event in executor.follow_run(run.run_id)]

    events = await asyncio.wait_for(collect(), timeout=5)
    offsets = [event["_offset"] for event in events]
    assert offsets == sorted(set(offsets))
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "meta"
    with sessions() as db:
        completed = db.get(ChatRun, run.run_id)
        message = db.get(ChatMessage, run.message_id)
        assert completed.status == "completed"
        assert completed.last_event_offset > offsets[-1]
        assert message.content == "public answer"


@pytest.mark.asyncio
async def test_public_follow_after_recovery_resets_partial_stream_and_keeps_offsets(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-partial-stream",
        message_id="msg-partial-stream",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )
    assert journal.claim(row.run_id, owner="dead-worker", lease_seconds=60)
    journal.append_operation(
        row.run_id,
        owner="dead-worker",
        operation_type="worker_started",
        phase="pre_model",
        safety="replayable",
    )
    for text in ("old partial one", "old partial two"):
        offset = journal.allocate_event_offset(row.run_id, owner="dead-worker")
        await executor._xadd_event(
            row.run_id,
            offset,
            {"type": "content", "delta": text, "chat_id": "chat-1"},
        )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "recovered answer"}
        yield {"type": "meta", "route": "main", "usage": {}}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

    assert await executor.recover_orphan_runs() == 1

    async def collect_from_crash_offset():
        return [event async for event in executor.follow_run(row.run_id, from_offset=2)]

    events = await asyncio.wait_for(collect_from_crash_offset(), timeout=5)
    assert events[0]["_offset"] == 3
    assert all(event["_offset"] > 2 for event in events)
    assert any(event.get("reason") == "run_recovered" for event in events)
    assert events[-1]["type"] == "meta"
    raw_entries = await redis.xrange(
        run_event_stream.redis_stream_key(row.run_id), min="-", max="+"
    )
    decoded = [json.loads(fields["data"]) for _entry_id, fields in raw_entries]
    assert all("old partial" not in str(event) for event in decoded)
    assert decoded[-1]["type"] == executor._TERMINAL_TYPE


@pytest.mark.asyncio
async def test_active_run_probe_replays_the_complete_existing_prefix(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-refresh-replay",
        message_id="msg-refresh-replay",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(row.run_id, owner="live-worker", lease_seconds=60)
    for text in ("already produced one", "already produced two"):
        offset = journal.allocate_event_offset(row.run_id, owner="live-worker")
        await executor._xadd_event(
            row.run_id,
            offset,
            {"type": "content", "delta": text, "chat_id": "chat-1"},
        )

    with sessions() as db:
        response = chat_run_views.chat_active_run(
            "chat-1",
            user=UserContext(
                user_id="user-1",
                user_center_id="center-1",
                username="tester",
            ),
            db=db,
        )

    replay_from = response["data"]["last_event_offset"]

    async def collect_existing_prefix():
        follower = executor.follow_run(row.run_id, from_offset=replay_from)
        try:
            return [await anext(follower), await anext(follower)]
        finally:
            await follower.aclose()

    events = await asyncio.wait_for(collect_existing_prefix(), timeout=1)
    assert replay_from == 0
    assert [event["_offset"] for event in events] == [1, 2]
    assert [event["delta"] for event in events] == [
        "already produced one",
        "already produced two",
    ]


@pytest.mark.asyncio
async def test_lease_heartbeat_fails_closed_when_renew_raises(monkeypatch):
    class ExplodingJournal:
        def renew(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(executor, "_journal", lambda: ExplodingJournal())
    monkeypatch.setattr(executor, "_RUN_LEASE_HEARTBEAT_SEC", 0.0)
    lease_lost = asyncio.Event()
    worker = asyncio.create_task(asyncio.Event().wait())
    heartbeat = asyncio.create_task(
        executor._lease_heartbeat("run-heartbeat", "worker", worker, lease_lost)
    )

    await asyncio.wait_for(heartbeat, timeout=1)
    await asyncio.sleep(0)
    assert lease_lost.is_set()
    assert worker.cancelled()


@pytest.mark.asyncio
async def test_periodic_recovery_retries_a_lease_that_was_live_at_startup(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-live-at-startup",
        message_id="msg-live-at-startup",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
            },
        },
    )
    assert journal.claim(row.run_id, owner="crashed-process", lease_seconds=60)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == row.run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) + timedelta(milliseconds=60)}
        )
        db.commit()
    registered = asyncio.Event()

    def register(_run_id, coro, *, name):
        coro.close()
        registered.set()

    monkeypatch.setattr(executor, "_register_run_task", register)
    monkeypatch.setattr(executor, "_RUN_RECOVERY_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(executor, "_STALE_REAPER_INTERVAL_SEC", 60.0)
    loop_task = asyncio.create_task(executor.run_stale_reaper_loop())
    try:
        await asyncio.wait_for(registered.wait(), timeout=1)
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task


@pytest.mark.asyncio
async def test_reaper_terminal_cancels_worker_without_late_user_cancel_projection(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    entered = asyncio.Event()

    async def blocked_workflow(**_kwargs):
        entered.set()
        yield {"type": "model_progress"}
        await asyncio.Event().wait()

    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "astream_chat_workflow", blocked_workflow)
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
    await asyncio.wait_for(entered.wait(), timeout=1)
    worker = executor._active_runs[run.run_id]
    hard_expired_at = datetime.now(timezone.utc) - timedelta(
        seconds=executor._HARD_MAX_AGE_SEC + 60
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == run.run_id).update(
            {"started_at": hard_expired_at, "created_at": hard_expired_at}
        )
        db.commit()

    assert await executor.reap_stale_runs() == 1
    await asyncio.wait_for(worker, timeout=1)
    entries = await redis.xrange(run_event_stream.redis_stream_key(run.run_id), min="-", max="+")
    events = [json.loads(fields["data"]) for _entry_id, fields in entries]
    assert sum(event.get("type") == executor._TERMINAL_TYPE for event in events) == 1
    assert not any(event.get("_cancelled") for event in events)
    assert any("长时间无响应" in str(event.get("error") or "") for event in events)
    with sessions() as db:
        assert db.get(ChatRun, run.run_id).status == "failed"


@pytest.mark.asyncio
async def test_follow_run_survives_none_stream_reads(recovery_env, monkeypatch):
    """A backend read that yields nothing may answer None rather than [].

    follow_run must treat both as "no events yet" instead of failing the SSE
    stream with "流式响应中断". The Redis client now sits behind
    orchestration.run_event_stream, so that is where the empty read enters.
    """

    class _NoneStreamRedis:
        async def xrange(self, *a, **k):
            return None

        async def xread(self, *a, **k):
            return None

    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: _NoneStreamRedis())
    journal = RunJournal(recovery_env)
    journal.accept(
        run_id="run-none-stream",
        message_id="msg-none-stream",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim("run-none-stream", owner="w", lease_seconds=60)
    assert journal.complete("run-none-stream", owner="w", status="failed")

    events = []
    async for event in executor.follow_run("run-none-stream", from_offset=0):
        events.append(event)
    assert events == []


async def test_follower_drops_the_poisoned_connection_when_xread_fails(recovery_env, monkeypatch):
    """A failed XREAD must not blind the follower for the rest of the run.

    The parse error leaves that pooled connection desynchronised, so every
    later read on it fails too. Retrying without dropping it means the follower
    yields nothing until the run goes terminal — the desktop client then sits
    on "starting task" for minutes while the agent is already answering.
    """
    inner = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    dropped = asyncio.Event()
    disconnects = []

    class _Pool:
        def __init__(self, owner) -> None:
            self.owner = owner

        async def disconnect(self, inuse_connections=True):
            disconnects.append(inuse_connections)
            self.owner.poisoned = False
            dropped.set()

        def __getattr__(self, name):
            return getattr(inner.connection_pool, name)

    class _Poisoned:
        """Keeps failing XREAD until the pool hands out a fresh connection."""

        def __init__(self) -> None:
            self.poisoned = True
            self.connection_pool = _Pool(self)

        async def xread(self, *args, **kwargs):
            if self.poisoned:
                raise AttributeError("'list' object has no attribute 'items'")
            return await inner.xread(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    client = _Poisoned()
    gate = asyncio.Event()

    async def workflow(**_kwargs):
        yield {"type": "content", "delta": "first"}
        await gate.wait()
        yield {"type": "content", "delta": "second"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": False,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: client)
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "core.services.artifact_service.persist_artifacts",
        lambda *_args, **_kwargs: None,
    )

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

    async def collect():
        return [event async for event in executor.follow_run(run.run_id)]

    follower = asyncio.create_task(collect())
    await asyncio.wait_for(dropped.wait(), timeout=5)
    gate.set()
    events = await asyncio.wait_for(follower, timeout=10)

    assert disconnects == [False]
    assert "second" in [event.get("delta") for event in events if event["type"] == "content"]
    assert events[-1]["type"] == "meta"
