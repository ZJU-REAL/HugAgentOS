from tests.features.desktop.observability_support import (bridge_identity, pytest, create_engine, sessionmaker)

def test_gateway_unknown_usage_keeps_desktop_usage_and_parent(store):
    from features.desktop.observability.projection import ingest, list_records

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
    from features.desktop.observability.sync import acknowledge, install_capture, prepare_batch
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
    from core.harness.capability_errors import CloudUnavailable
    from core.db.models import ChatSession, UserShadow
    from features.desktop.bridge import client as bridge
    from features.desktop.observability.sync import DesktopSyncWorker, install_capture, prepare_batch

    identity = {
        "cloud_base": "http://cloud",
        "subject": "alice",
        "device_id": "d",
        "shell_user_center_id": "cloud:alice",
    }
    state = {"cloud_base": "http://cloud", "token": "alice-credential", "device_id": "d"}
    current = [state]
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: current[0])
    monkeypatch.setattr(bridge_identity, 'get_identity_state', lambda: identity)
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
    from features.desktop.bridge import client as bridge
    from features.desktop.observability.sync import DesktopSyncWorker, install_capture

    identity = {
        "cloud_base": "http://cloud",
        "subject": "u",
        "device_id": "d",
        "shell_user_center_id": "cloud:u",
    }
    state = {"cloud_base": "http://cloud", "token": "test", "device_id": "d"}
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: state)
    monkeypatch.setattr(bridge_identity, 'get_identity_state', lambda: identity)
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
    from features.desktop.models import DesktopOutbox
    from features.desktop.observability.sync import install_capture
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
