"""Command wake transport, snapshot races and authentication boundaries."""

import asyncio
import json
import shutil
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.chat.runtime.admission as admission
import core.db.engine as engine
import core.sandbox.commands.events as commands_events
import core.sandbox.commands.events as events
import core.sandbox.commands.notifications as notifications
import core.sandbox.commands.subscription as commands_subscription
import core.sandbox.commands.subscription as subscription
import pytest
from core.db import models
from core.chat.sequencer import ChatSequencer
from core.sandbox.commands import delivery, store
from fastapi import HTTPException
from redis.asyncio import Redis
from starlette.requests import Request
from tests.orchestration.test_background_commands import command_db, request


async def test_memory_event_wakes_immediately_and_isolates_topics(monkeypatch):
    bus = events.MemoryCommandEvents()
    cursor = await bus.tail("u", "c")
    waiter = asyncio.create_task(bus.wait("u", "c", cursor, timeout=1))
    await asyncio.sleep(0)
    await bus.publish("other", "c", "foreign-user")
    await bus.publish("u", "other", "foreign-chat")
    assert not waiter.done()
    await bus.publish("u", "c", "wake")
    _, runs, _ = await asyncio.wait_for(waiter, .5)
    assert runs == ["wake"]
    monkeypatch.setattr(commands_events, "MAX_EVENTS", 2)
    bus = events.MemoryCommandEvents()
    for index in range(3):
        await bus.publish("u", "c", str(index))
    _, runs, resync = await bus.wait("u", "c", 0)
    assert runs == ["1", "2"] and resync
    monkeypatch.setattr(commands_events, "TTL_SECONDS", -.1)
    await bus.publish("u", "c", "expired")
    assert await bus.tail("u", "c") == 0


async def test_redis_two_instances_deliver_and_isolate(tmp_path, monkeypatch):
    executable = shutil.which("redis-server")
    assert executable, "Real Redis binary required for command push verification"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen([executable, "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clients = [Redis(host="127.0.0.1", port=port, decode_responses=True) for _ in range(2)]
    try:
        for _ in range(100):
            try:
                await clients[0].ping()
                break
            except Exception:
                await asyncio.sleep(.02)
        monkeypatch.setattr(commands_events, "get_redis", lambda blocking=False: clients[int(blocking)])
        publisher, subscriber = events.RedisCommandEvents(), events.RedisCommandEvents()
        cursor = await subscriber.tail("u", "c")
        waiting = asyncio.create_task(subscriber.wait("u", "c", cursor, timeout=1))
        await asyncio.sleep(.03)
        await publisher.publish("other", "c", "foreign")
        assert not waiting.done()
        await publisher.publish("u", "c", "wake")
        _, runs, _ = await asyncio.wait_for(waiting, .5)
        assert runs == ["wake"]
        assert 0 < await clients[0].ttl(events.topic("u", "c")) <= events.TTL_SECONDS
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
        process.terminate()
        process.wait(timeout=5)


async def test_snapshot_tail_race_and_auth_revocation(monkeypatch):
    bus = events.MemoryCommandEvents()
    monkeypatch.setattr(commands_subscription, "get_command_events", lambda: bus)
    monkeypatch.setattr(commands_subscription, "HEARTBEAT_SECONDS", .01)
    authorized = True
    async def authorize(*args):
        if not authorized:
            raise HTTPException(401, "expired")
        return "u"
    monkeypatch.setattr(commands_subscription, "authorize", authorize)
    def snapshot(chat_id, user_id):
        # Publication happens after captured cursor, while DB snapshot is read.
        asyncio.run(bus.publish(user_id, chat_id, "during-snapshot"))
        return []
    monkeypatch.setattr(commands_subscription, "snapshot", snapshot)
    stream = subscription.subscribe(None, "c", "u")
    assert json.loads((await anext(stream))[6:])["run_ids"] == []
    assert json.loads((await anext(stream))[6:])["run_ids"] == ["during-snapshot"]
    authorized = False
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_event_authorization_rechecks_identity_scope_and_edit(command_db, monkeypatch):
    req = Request({"type":"http", "headers":[]})
    user = SimpleNamespace(user_id="u", api_key_id=None, public_chat_visitor=None)
    monkeypatch.setattr(commands_subscription, "get_current_user", AsyncMock(side_effect=lambda *a, **kw: user))
    assert await subscription.authorize(req, "c") == "u"
    user.user_id = "other"
    with pytest.raises(HTTPException) as error:
        await subscription.authorize(req, "c")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await subscription.authorize(req, "c", "u")
    assert error.value.status_code == 401
    user.user_id, user.api_key_id = "u", "scoped-key"
    with pytest.raises(HTTPException) as error:
        await subscription.authorize(req, "c")
    assert error.value.status_code == 403
    user.api_key_id = None
    with command_db() as db:
        chat = db.get(models.ChatSession, "c")
        from datetime import datetime, timezone
        chat.deleted_at = datetime.now(timezone.utc)
        db.commit()
    with pytest.raises(HTTPException):
        await subscription.authorize(req, "c")


async def test_publish_failure_retries_without_starting_run_again(command_db, monkeypatch):
    pass
    with command_db() as db:
        source = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start", request_payload={}).run
        source.status, source.writer_slot = "completed", None
        source.recovery_snapshot = {"kind":"chat", "worker_args":{"context":{"chat_mode":"medium","enable_thinking":True}}}
        source_id = source.run_id
        db.commit()
    row = store.create(request(), "c", source_id)
    store.attach(row["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(row["task_id"], "owner", {"status":"exited","exit_code":0})
    run_id = delivery.prepare([store.snapshot(row["task_id"])], "dispatcher")
    starts = []
    async def start_run(**kwargs):
        starts.append(kwargs)
        with command_db() as db:
            db.get(models.ChatRun, run_id).recovery_snapshot = {"kind":"chat","worker_args":{}}
            db.commit()
        # Model execution can read the terminal task before its wake event is
        # published. That read must not cancel the accepted delivery.
        assert store.read(row["task_id"], "u", "c")["status"] == "exited"
        assert store.snapshot(row["task_id"])["notice_status"] == "reserved"
    monkeypatch.setattr(admission, "start_run", start_run)
    publisher = SimpleNamespace(publish=AsyncMock(side_effect=[RuntimeError("Redis down"), None]))
    monkeypatch.setattr(commands_events, "get_command_events", lambda: publisher)
    with pytest.raises(RuntimeError, match="Redis down"):
        await notifications.launch(run_id)
    assert store.snapshot(row["task_id"])["notice_status"] == "reserved"
    assert subscription.snapshot("c", "u") == [run_id]
    assert subscription.snapshot("c", "other") == []
    assert any(record["task_id"] == row["task_id"] for record in store.pending_notices())
    delivery.release_notice(row["task_id"], "dispatcher")
    await notifications.dispatch()
    assert len(starts) == 1
    assert publisher.publish.await_count == 2
    assert store.snapshot(row["task_id"])["notice_status"] == "delivered"


async def test_real_http_sse_is_authenticated_and_pushes_without_polling(command_db, monkeypatch):
    import threading

    import httpx
    import uvicorn
    from api.routes.v1.commands import router
    from fastapi import FastAPI
    bus = events.MemoryCommandEvents()
    monkeypatch.setattr(commands_subscription, "get_command_events", lambda: bus)
    monkeypatch.setattr(commands_subscription, "HEARTBEAT_SECONDS", .03)
    revoked = False
    async def authenticated(req, credentials):
        if credentials is None or revoked:
            raise HTTPException(401, "Authentication expired")
        return SimpleNamespace(user_id=credentials.credentials,
            api_key_id=None, public_chat_visitor=None)
    monkeypatch.setattr(commands_subscription, "get_current_user", authenticated)
    app = FastAPI()
    app.include_router(router)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            assert (await client.get("/v1/commands/events?chat_id=c")).status_code == 401
            assert (await client.get("/v1/commands/events?chat_id=c",
                headers={"Authorization":"Bearer other"})).status_code == 404
            async with client.stream("GET", "/v1/commands/events?chat_id=c",
                    headers={"Authorization":"Bearer u"}) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = response.aiter_lines()
                async def next_data():
                    async for line in lines:
                        if line.startswith("data: "):
                            return json.loads(line[6:])
                    raise StopAsyncIteration
                assert (await next_data())["run_ids"] == []
                await bus.publish("u", "c", "pushed")
                assert (await asyncio.wait_for(next_data(), .5))["run_ids"] == ["pushed"]
                revoked = True
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(next_data(), .5)
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 3)
        assert not thread.is_alive()


async def test_memory_publish_unregisters_closed_subscriber_loop():
    bus = events.MemoryCommandEvents()
    closed = asyncio.new_event_loop()
    future = closed.create_future()
    key = events.topic("u", "c")
    bus.waiters[key] = {(closed, future)}
    closed.close()
    await bus.publish("u", "c", "wake")
    assert key not in bus.waiters
    _, runs, _ = await bus.wait("u", "c", 0, timeout=.1)
    assert runs == ["wake"]


def test_snapshot_is_bounded_and_returns_latest_runs(command_db, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(commands_subscription, "MAX_EVENTS", 2)
    run_ids = []
    for index in range(3):
        with command_db() as db:
            run = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
                user_content="wake", request_payload={}).run
            run.status, run.writer_slot = "completed", None
            run.recovery_snapshot = {"kind":"chat", "worker_args":{}}
            run.created_at = datetime(2026, 1, index + 1, tzinfo=timezone.utc)
            run_ids.append(run.run_id)
            db.commit()
        command = store.create(request(), "c", None)
        with store.locked(command["task_id"]) as (_, row):
            row.status, row.delivery_mode, row.notice_status = "completed", "followup", "delivered"
            row.notice_run_id = run_ids[-1]
    assert subscription.snapshot("c", "u") == list(reversed(run_ids[-2:]))


@pytest.mark.parametrize(("mode", "thinking"), [("fast", False), ("high", True)])
def test_followup_active_run_inherits_execution_mode(command_db, monkeypatch, mode, thinking):
    from core.auth.backend import UserContext
    from core.chat.operations.runs import chat_active_run
    pass
    monkeypatch.setattr(engine, "SessionLocal", command_db)
    with command_db() as db:
        source = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="start",
            request_payload={"chat_mode":"medium" if mode == "fast" else "fast",
                             "enable_thinking":not thinking}).run
        source.status, source.writer_slot = "completed", None
        source.recovery_snapshot = {"kind":"chat", "worker_args":{
            "context":{"chat_mode":mode, "enable_thinking":thinking}}}
        source_id = source.run_id
        db.commit()
    command = store.create(request(), "c", source_id)
    store.attach(command["task_id"], "owner", {"status":"running","session_id":"p"}, None)
    store.collect(command["task_id"], "owner", {"status":"exited","exit_code":0})
    followup_id = delivery.prepare([store.snapshot(command["task_id"])], "dispatcher")
    with command_db() as db:
        followup = db.get(models.ChatRun, followup_id)
        assert followup.request_payload["kind"] == "chat"
        assert followup.request_payload["wake_reason"] == "command_finish"
        assert followup.request_payload["chat_mode"] == mode
        assert followup.request_payload["enable_thinking"] is thinking
        response = chat_active_run("c",
            user=UserContext(user_id="u", user_center_id="u", username="test"), db=db)
        assert response["data"]["run_id"] == followup_id
        assert response["data"]["enable_thinking"] is thinking
        assert response["data"]["kind"] == "chat"
        from api.routes.v1.chats.run_views import chat_run_subscription
        native = chat_run_subscription(followup_id,
            user=UserContext(user_id="u", user_center_id="u", username="test"))
        assert native.status_code == 200
        assert native.media_type == "text/event-stream"
