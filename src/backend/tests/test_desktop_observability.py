"""Cloud management copies survive delivery retries and never become runnable chats."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def store(tmp_path):
    from core.db.models.observability import DesktopOutbox, DesktopRecord, DesktopSyncDevice

    engine = create_engine("sqlite:///" + str(tmp_path / "cloud.db"))
    from core.db.models import UserShadow

    for model in (DesktopRecord, DesktopSyncDevice, DesktopOutbox, UserShadow):
        model.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_duplicate_and_stale_delivery_keep_newest_chat(store):
    from core.services.desktop_observability import ingest, list_records

    def batch(version, content):
        return {
            "events": [
                {
                    "kind": "message",
                    "object_id": "m1",
                    "revision": version,
                    "payload": {"chat_id": "c1", "role": "user", "content": content},
                }
            ]
        }

    with store() as db:
        ingest(db, "alice", "laptop", batch(2, "latest"))
        ingest(db, "alice", "laptop", batch(2, "latest"))
        ingest(db, "alice", "laptop", batch(1, "old"))
        rows, total = list_records(db, kind="message", user_id="alice")
        assert total == 1
        assert rows[0]["payload"]["content"] == "latest"
        assert list_records(db, kind="message", user_id="bob")[1] == 0
        ingest(db, "bob", "laptop", batch(1, "bob's message"))
        assert (
            list_records(db, kind="message", user_id="bob")[0][0]["payload"]["content"]
            == "bob's message"
        )


def test_capture_upload_and_conditional_ack_survive_retry(tmp_path):
    import asyncio

    from core.db.models import ChatMessage, ChatSession, UserShadow
    from core.db.models.observability import DesktopOutbox
    from core.services.desktop_observability_sync import acknowledge, install_capture, prepare_batch

    engine = create_engine("sqlite:///" + str(tmp_path / "local.db"))
    from core.db.models import ChatRun

    for m in (UserShadow, ChatSession, ChatMessage, ChatRun, DesktopOutbox):
        m.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    identity = {
        "cloud_base": "https://cloud.example",
        "subject": "alice",
        "device_id": "device",
        "shell_user_center_id": "cloud:cloud.example:443:center",
    }
    remove = install_capture(factory, lambda: identity)
    try:
        with factory() as db:
            db.add(
                UserShadow(
                    user_id="local-alice",
                    username="Alice",
                    user_center_id=identity["shell_user_center_id"],
                )
            )
            db.add(ChatSession(chat_id="c", user_id="local-alice", title="Desktop task"))
            db.add(
                ChatMessage(message_id="m", chat_id="c", role="user", chat_seq=1, content="hello")
            )
            db.commit()
        with factory() as db:
            first = prepare_batch(db, identity)
        assert {e["kind"] for e in first["events"]} == {"session", "message"}
        with factory() as db:
            row = db.get(ChatMessage, "m")
            row.content = "newer"
            db.commit()
        with factory() as db:
            acknowledge(db, identity, {"acknowledged": first["events"]})
            retry = prepare_batch(db, identity)
        assert len(retry["events"]) == 1
        assert retry["events"][0]["payload"]["content"] == "newer"
        with factory() as db:
            assert prepare_batch(db, {**identity, "subject": "bob"})["events"] == []
    finally:
        remove()


def test_cloud_gateway_and_desktop_receipt_are_one_call(store):
    from core.services.desktop_observability import ingest, list_records

    event = {
        "kind": "tool",
        "object_id": "result-1",
        "revision": 1,
        "payload": {
            "tool_name": "search",
            "chat_id": "c",
            "status": "success",
            "tool_args": {"api_key": "secret-value"},
        },
    }
    with store() as db:
        ingest(db, "alice", "device", {"events": [event]})
        ingest(db, "alice", "device", {"events": [event]}, observation="gateway")
        rows, total = list_records(db, kind="tool", user_id="alice")
        assert total == 1
        assert rows[0]["observation"] == "gateway"
        assert rows[0]["payload"]["tool_args"]["api_key"] == "[redacted]"


def test_gateway_stream_arrives_before_log_writer_and_extracts_usage(monkeypatch):
    import asyncio

    from core.services import desktop_gateway_observability as obs
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    captured = []
    monkeypatch.setattr(obs, "submit", lambda *args: captured.append(args))
    app = FastAPI()
    app.router.route_class = obs.ObservedGatewayRoute

    @app.post("/gateway/models/model/chat/completions")
    async def handler(request: Request):
        request.state.desktop_observation_user = "alice"

        async def chunks():
            assert not captured
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            assert not captured
            yield b'data: {"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'

        return StreamingResponse(chunks(), media_type="text/event-stream")

    response = TestClient(app).post(
        "/gateway/models/model/chat/completions",
        headers={"x-desktop-device-id": "d", "x-observation-call-id": "try-1"},
    )
    assert response.status_code == 200
    assert "hello" in response.text
    assert len(captured) == 1
    assert captured[0][-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert captured[0][-1]["status"] == "success"


@pytest.fixture
def local_store(tmp_path):
    from core.db.models import UserShadow
    from core.db.models.observability import DesktopOutbox
    from core.services.desktop_observability_sync import SOURCES

    engine = create_engine("sqlite:///" + str(tmp_path / "desktop.db"))
    for model in {UserShadow, DesktopOutbox, *(m for m, _ in SOURCES.values())}:
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    return factory


def test_real_upload_endpoint_authorization_and_size_limit(store, monkeypatch):
    from api.routes.v1 import desktop_observability as route
    from core.db.engine import get_db
    from core.services.desktop_observability import MAX_BATCH_BYTES
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CONFIG_TOKEN", "test-config")
    monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", raising=False)
    monkeypatch.setattr(route, "SessionLocal", store)

    def database():
        with store() as db:
            yield db

    app = FastAPI()
    app.include_router(route.router, prefix="/api")
    app.include_router(route.admin_router, prefix="/api")
    app.dependency_overrides[get_db] = database
    client = TestClient(app)
    assert (
        client.post("/api/v1/desktop/observability/batch", json={"events": []}).status_code == 401
    )
    assert client.get("/api/v1/admin/desktop-observability/records").status_code == 401
    app.dependency_overrides[route._require_capability_user] = lambda: "alice"
    event = {
        "kind": "message",
        "object_id": "m",
        "revision": 1,
        "payload": {"content": "private conversation", "user_id": "victim", "role": "user"},
    }
    response = client.post(
        "/api/v1/desktop/observability/batch",
        headers={"x-desktop-device-id": "d"},
        json={"events": [event]},
    )
    assert response.status_code == 200, response.text
    data = client.get(
        "/api/v1/admin/desktop-observability/records",
        headers={"Authorization": "Bearer test-config"},
    ).json()["data"]
    assert data["pagination"]["total_items"] == 1
    assert data["items"][0]["user_id"] == "alice"
    assert "user_id" not in data["items"][0]["payload"]
    assert (
        client.post(
            "/api/v1/desktop/observability/batch",
            headers={"x-desktop-device-id": "d"},
            content=b"x" * (MAX_BATCH_BYTES + 1),
        ).status_code
        == 413
    )


def test_offline_restart_and_http_delivery_to_cloud(local_store, store, monkeypatch):
    import asyncio

    import httpx
    from api.routes.v1 import desktop_observability as route
    from core.db.models import (
        ChatMessage,
        ChatSession,
        SkillCallLog,
        SubAgentCallLog,
        ToolCallLog,
        UserShadow,
    )
    from core.services import desktop_cloud_bridge as bridge
    from core.services.desktop_observability import list_records
    from core.services.desktop_observability_sync import (
        DesktopSyncWorker,
        install_capture,
        prepare_batch,
    )
    from fastapi import FastAPI

    identity = {
        "cloud_base": "http://cloud",
        "subject": "alice",
        "device_id": "d",
        "shell_user_center_id": "cloud:cloud:80:alice",
    }
    state = {"cloud_base": "http://cloud", "token": "test-credential", "device_id": "d"}
    monkeypatch.setattr(bridge, "get_state", lambda: state)
    monkeypatch.setattr(bridge, "get_identity_state", lambda: identity)
    remove = install_capture(local_store, lambda: identity)
    try:
        with local_store() as db:
            db.add(
                UserShadow(
                    user_id="local",
                    username="Alice",
                    user_center_id=identity["shell_user_center_id"],
                )
            )
            db.add(ChatSession(chat_id="c", user_id="local", title="Research"))
            db.add(
                ChatMessage(message_id="m", chat_id="c", chat_seq=1, role="user", content="Hello")
            )
            db.add(
                ToolCallLog(
                    id="t", user_id="local", chat_id="c", tool_name="search", status="success"
                )
            )
            db.add(
                SkillCallLog(
                    id="s",
                    user_id="local",
                    chat_id="c",
                    skill_id="cloud-skill",
                    skill_source="cloud",
                    invocation_type="view",
                    status="success",
                )
            )
            db.add(
                SubAgentCallLog(
                    id="a",
                    user_id="local",
                    chat_id="c",
                    subagent_name="Researcher",
                    status="success",
                )
            )
            db.commit()

        async def offline(request):
            raise httpx.ConnectError("offline")

        worker = DesktopSyncWorker(local_store, transport=httpx.MockTransport(offline))
        with pytest.raises(httpx.ConnectError):
            asyncio.run(worker.sync_once())
        with local_store() as db:
            assert len(prepare_batch(db, identity)["events"]) == 5
        monkeypatch.setattr(route, "SessionLocal", store)
        app = FastAPI()
        app.include_router(route.router, prefix="/api")
        app.dependency_overrides[route._require_capability_user] = lambda: "alice"
        restarted = DesktopSyncWorker(local_store, transport=httpx.ASGITransport(app=app))
        asyncio.run(restarted.sync_once())
        asyncio.run(restarted.sync_once())
        with local_store() as db:
            assert prepare_batch(db, identity)["events"] == []
        with store() as db:
            assert list_records(db, user_id="alice")[1] == 5
            assert list_records(db, kind="message")[0][0]["payload"]["content"] == "Hello"
            assert list_records(db, kind="skill")[0][0]["payload"]["capability_origin"] == "cloud"
    finally:
        remove()


def test_unicode_large_records_and_secrets_are_safe(store):
    import json

    from core.services.desktop_observability import (
        MAX_EVENT_BYTES,
        ingest,
        list_records,
        public_payload,
    )

    payload = public_payload("message", {"content": "中" * 100000, "role": "assistant"})
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < MAX_EVENT_BYTES
    with store() as db:
        ingest(
            db,
            "u",
            "d",
            {"events": [{"kind": "message", "object_id": "m", "revision": 1, "payload": payload}]},
        )
        assert list_records(db, kind="message")[0][0]["payload"]["content"] == "中" * 100000
    payload = public_payload(
        "tool",
        {
            "tool_args": {"api_key": "hidden"},
            "tool_result": '{"api_key":"hidden"} Bearer abcdefghi -----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----',
        },
    )
    assert "hidden" not in json.dumps(payload)
    assert "abcdefghi" not in json.dumps(payload)
    assert "BEGIN PRIVATE KEY" not in json.dumps(payload)
    assert public_payload("message", {"content": "x" * 100001})["truncated"] is True


def test_gateway_unknown_usage_keeps_desktop_usage_and_parent(store):
    from core.services.desktop_observability import ingest, list_records

    with store() as db:
        ingest(
            db,
            "u",
            "d",
            {
                "events": [
                    {
                        "kind": "usage",
                        "object_id": "local-attempt",
                        "revision": 1,
                        "payload": {
                            "kind": "model",
                            "call_id": "attempt",
                            "prompt_tokens": 20,
                            "completion_tokens": 5,
                        },
                    }
                ]
            },
        )
        ingest(
            db,
            "u",
            "d",
            {
                "events": [
                    {
                        "kind": "model",
                        "object_id": "attempt",
                        "revision": 2,
                        "payload": {"usage_known": False, "status": "success"},
                    }
                ]
            },
            observation="gateway",
        )
        rows, total = list_records(db, kind="model")
        assert total == 1
        assert rows[0]["payload"]["usage"]["prompt_tokens"] == 20
        ingest(
            db,
            "u",
            "d",
            {
                "events": [
                    {
                        "kind": "tool",
                        "object_id": "t",
                        "revision": 1,
                        "payload": {"subagent_log_id": "parent", "status": "success"},
                    }
                ]
            },
        )
        ingest(
            db,
            "u",
            "d",
            {
                "events": [
                    {
                        "kind": "tool",
                        "object_id": "t",
                        "revision": 2,
                        "payload": {"status": "success", "duration_ms": 10},
                    }
                ]
            },
            observation="gateway",
        )
        assert list_records(db, kind="tool")[0][0]["payload"]["subagent_log_id"] == "parent"


def test_bulk_updates_are_uploaded_and_rollback_is_not(local_store):
    from core.db.models import ChatMessage, ChatSession, UserShadow
    from core.services.desktop_observability_sync import acknowledge, install_capture, prepare_batch
    from sqlalchemy import update

    identity = {
        "cloud_base": "https://cloud",
        "subject": "u",
        "device_id": "d",
        "shell_user_center_id": "cloud:center",
    }
    remove = install_capture(local_store, lambda: identity)
    try:
        with local_store() as db:
            db.add(UserShadow(user_id="local", username="U", user_center_id="cloud:center"))
            db.add(ChatSession(chat_id="c", user_id="local", title="Before"))
            db.add(
                ChatMessage(
                    message_id="m", chat_id="c", chat_seq=1, role="assistant", content="partial"
                )
            )
            db.commit()
        with local_store() as db:
            acknowledge(db, identity, {"acknowledged": prepare_batch(db, identity)["events"]})
        with local_store() as db:
            db.execute(
                update(ChatMessage).where(ChatMessage.message_id == "m").values(content="final")
            )
            db.commit()
        with local_store() as db:
            batch = prepare_batch(db, identity)
            assert len(batch["events"]) == 1
            assert batch["events"][0]["payload"]["content"] == "final"
            acknowledge(db, identity, {"acknowledged": batch["events"]})
        with local_store() as db:
            db.get(ChatMessage, "m").content = "rolled back"
            db.flush()
            db.rollback()
        with local_store() as db:
            assert prepare_batch(db, identity)["events"] == []
    finally:
        remove()


def test_account_switch_during_upload_does_not_ack_old_account(local_store, monkeypatch):
    import asyncio

    import httpx
    from core.capabilities.errors import CloudUnavailable
    from core.db.models import ChatSession, UserShadow
    from core.services import desktop_cloud_bridge as bridge
    from core.services.desktop_observability_sync import (
        DesktopSyncWorker,
        install_capture,
        prepare_batch,
    )

    identity = {
        "cloud_base": "http://cloud",
        "subject": "alice",
        "device_id": "d",
        "shell_user_center_id": "cloud:alice",
    }
    state = {"cloud_base": "http://cloud", "token": "alice-credential", "device_id": "d"}
    current = [state]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(bridge, "get_identity_state", lambda: identity)
    remove = install_capture(local_store, lambda: identity)
    try:
        with local_store() as db:
            db.add(UserShadow(user_id="local", username="Alice", user_center_id="cloud:alice"))
            db.add(ChatSession(chat_id="c", user_id="local", title="Alice only"))
            db.commit()

        async def switch(request):
            import json

            body = json.loads(request.content)
            assert request.headers["authorization"] == "Bearer alice-credential"
            current[0] = {**state, "token": "bob-credential"}
            return httpx.Response(200, json={"data": {"acknowledged": body["events"]}})

        with pytest.raises(CloudUnavailable):
            asyncio.run(
                DesktopSyncWorker(local_store, transport=httpx.MockTransport(switch)).sync_once()
            )
        with local_store() as db:
            assert len(prepare_batch(db, identity)["events"]) == 1
    finally:
        remove()


def test_blocked_upload_does_not_block_task_progress(local_store, monkeypatch):
    import asyncio

    import httpx
    from core.db.models import ChatSession, UserShadow
    from core.services import desktop_cloud_bridge as bridge
    from core.services.desktop_observability_sync import DesktopSyncWorker, install_capture

    identity = {
        "cloud_base": "http://cloud",
        "subject": "u",
        "device_id": "d",
        "shell_user_center_id": "cloud:u",
    }
    state = {"cloud_base": "http://cloud", "token": "test", "device_id": "d"}
    monkeypatch.setattr(bridge, "get_state", lambda: state)
    monkeypatch.setattr(bridge, "get_identity_state", lambda: identity)
    remove = install_capture(local_store, lambda: identity)
    try:
        with local_store() as db:
            db.add(UserShadow(user_id="local", username="U", user_center_id="cloud:u"))
            db.add(ChatSession(chat_id="c", user_id="local", title="Initial"))
            db.commit()

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            async def slow_cloud(request):
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"data": {"acknowledged": []}})

            worker = DesktopSyncWorker(local_store, transport=httpx.MockTransport(slow_cloud))
            upload = asyncio.create_task(worker.sync_once())
            await asyncio.wait_for(entered.wait(), timeout=2)

            def advance():
                with local_store() as db:
                    db.get(ChatSession, "c").title = "Task continued"
                    db.commit()

            await asyncio.wait_for(asyncio.to_thread(advance), timeout=1)
            assert not upload.done()
            release.set()
            await upload

        asyncio.run(scenario())
    finally:
        remove()


@pytest.mark.parametrize("finish", ["commit", "rollback"])
@pytest.mark.parametrize("operation", ["flush", "update", "delete"])
def test_capture_identity_is_stable_within_transaction_and_refreshes_on_reuse(
    local_store, finish, operation
):
    from core.db.models import ChatSession, UserShadow
    from core.db.models.observability import DesktopOutbox
    from core.services.desktop_observability_sync import install_capture
    from sqlalchemy import update, delete

    alice = dict(
        cloud_base="https://cloud",
        subject="alice",
        device_id="d",
        shell_user_center_id="center-alice",
    )
    bob = dict(
        cloud_base="https://cloud", subject="bob", device_id="d", shell_user_center_id="center-bob"
    )
    with local_store() as db:
        for name in ("alice", "bob"):
            db.add(UserShadow(user_id=name, username=name, user_center_id="center-" + name))
        db.add(ChatSession(chat_id="existing", user_id="alice", title="before"))
        db.commit()
    identity = alice
    remove = install_capture(local_store, lambda: identity)
    try:
        with local_store() as db:
            db.begin()
            identity = bob  # Source transaction still belongs to its original account.
            if operation == "flush":
                db.add(ChatSession(chat_id="new-alice", user_id="alice", title="Alice"))
                db.flush()
            else:
                stmt = (
                    update(ChatSession).values(title="after")
                    if operation == "update"
                    else delete(ChatSession)
                )
                db.execute(stmt.where(ChatSession.chat_id == "existing"))
            getattr(db, finish)()
            # Reuse the same Session. Neither commit nor rollback may retain Alice.
            with db.begin_nested():
                db.add(ChatSession(chat_id="new-bob", user_id="bob", title="Bob"))
                db.flush()
            db.commit()
        with local_store() as db:
            rows = db.query(DesktopOutbox).all()
            assert [(r.subject, r.object_id) for r in rows if r.subject == "bob"] == [
                ("bob", "new-bob")
            ]
            assert len([r for r in rows if r.subject == "alice"]) == (
                1 if finish == "commit" else 0
            )
    finally:
        remove()
