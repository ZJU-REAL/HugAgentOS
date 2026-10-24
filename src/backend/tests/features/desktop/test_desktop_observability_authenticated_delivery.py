from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.api import desktop_capability
"""Signed cloud authentication and shell publication recover a durable desktop outbox."""
from features.desktop.api import desktop_capability_state

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_expired_credential_renews_via_apis_and_preserves_account_outboxes(tmp_path, monkeypatch):
    from features.desktop.api import desktop_capability as control
    from features.desktop.api import desktop_observability as upload
    from core.auth import session
    from core.config.settings import settings
    from core.db.models import ChatSession, ContentBlock, UserShadow
    from features.desktop.models import DesktopOutbox, DesktopRecord, DesktopSyncDevice
    from features.desktop.capability import service as capability
    from features.desktop.bridge import client as bridge
    from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills
    from features.desktop.observability.projection import list_records
    from features.desktop.observability.sync import SOURCES, DesktopSyncWorker, install_capture, prepare_batch
    from fastapi import FastAPI

    local_engine = create_engine("sqlite:///" + str(tmp_path / "local.db"))
    cloud_engine = create_engine("sqlite:///" + str(tmp_path / "cloud.db"))
    for model in {UserShadow, DesktopOutbox, *(m for m, _ in SOURCES.values())}:
        model.__table__.create(local_engine)
    for model in (UserShadow, ContentBlock, DesktopRecord, DesktopSyncDevice):
        model.__table__.create(cloud_engine)
    local_factory, cloud_factory = (
        sessionmaker(bind=local_engine),
        sessionmaker(bind=cloud_engine),
    )
    monkeypatch.setattr(capability, "SessionLocal", cloud_factory)
    monkeypatch.setattr(capability, "_secret_cache", None)
    monkeypatch.setattr(upload, "SessionLocal", cloud_factory)
    monkeypatch.setattr(
        session, "settings", SimpleNamespace(session=replace(settings.session, store_type="memory"))
    )
    monkeypatch.setattr(session.process_state(), 'MEMORY_SESSIONS', {})
    monkeypatch.delenv("DESKTOP_CAPABILITY_ISSUER", raising=False)
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "isolated-shell-process-secret")
    monkeypatch.setattr(bridge_client_state, 'state', None)
    monkeypatch.setattr(bridge_client_state, 'state_loaded', True)
    monkeypatch.setattr(bridge_client_state, 'credential_rejected', "")
    # These manage unrelated capability files/background network activity.
    monkeypatch.setattr(bridge_identity, 'purge_persisted_state', lambda: None)
    monkeypatch.setattr(bridge_identity, 'clear_partial_selection', lambda: None)
    monkeypatch.setattr(bridge_identity, 'rebuild_identity_views', lambda: None)
    monkeypatch.setattr(desktop_cloud_skills, "on_account_switch", lambda: None)
    monkeypatch.setattr(desktop_cloud_bundles, "on_account_switch", lambda: None)
    refreshes = []
    monkeypatch.setattr(bridge_client, 'refresh_manifest_async', lambda **kw: refreshes.append(kw))
    cloud_clock = [time.time()]
    monkeypatch.setattr(capability, "time", SimpleNamespace(time=lambda: cloud_clock[0]))
    cloud, local = FastAPI(), FastAPI()
    cloud.include_router(desktop_capability.router, prefix="/api")
    cloud.include_router(upload.router, prefix="/api")
    local.include_router(desktop_capability.router, prefix="/api")
    remove = install_capture(local_factory, bridge_identity.get_identity_state)

    async def scenario():
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=cloud), base_url="http://cloud"
            ) as cloud_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=local), base_url="http://local"
            ) as local_client,
        ):
            cookies = {}
            for user in ("alice", "bob"):
                cookies[user] = await session.create_session(
                    dict(user_id=user, user_center_id=f"center-{user}", username=user)
                )

            async def issue(user):
                cloud_client.cookies.set(settings.session.cookie_name, cookies[user])
                reply = await cloud_client.post(
                    "/api/v1/desktop/capability/token", json={"device_id": "d"}
                )
                assert reply.status_code == 200, reply.text
                return reply.json()["data"]

            async def publish(data):
                reply = await local_client.post(
                    "/api/v1/desktop/capability/cloud-bridge",
                    headers={"Authorization": "Bearer isolated-shell-process-secret"},
                    json=dict(
                        cloud_base="http://cloud",
                        token=data["token"],
                        expires_in=data["expires_in"],
                        device_id="d",
                    ),
                )
                assert reply.status_code == 200, reply.text

            expired = await issue("alice")
            await publish(expired)
            alice_identity = bridge_identity.get_identity_state()
            with local_factory() as db:
                db.add(
                    UserShadow(
                        user_id="local-alice",
                        username="Alice",
                        user_center_id=alice_identity["shell_user_center_id"],
                    )
                )
                db.add(
                    ChatSession(
                        chat_id="c", user_id="local-alice", title="retained private conversation"
                    )
                )
                db.commit()
            worker = DesktopSyncWorker(local_factory, transport=httpx.ASGITransport(app=cloud))
            # The cloud can reject expiry before the client observes it (clock skew
            # or a request queued across expiry). No synthetic HTTP refusal is used.
            cloud_clock[0] += expired["expires_in"] + 1
            with pytest.raises(httpx.HTTPStatusError) as rejected:
                await worker.sync_once()
            assert rejected.value.response.status_code == 401
            assert bridge_client_state.credential_rejected
            with local_factory() as db:
                assert len(prepare_batch(db, alice_identity)["events"]) == 1
            with cloud_factory() as db:
                assert list_records(db, user_id="alice")[1] == 0
            before_refresh = len(refreshes)
            renewed = await issue("alice")
            assert renewed["token"] != expired["token"]
            await publish(renewed)
            assert len(refreshes) == before_refresh + 1
            assert not bridge_client_state.credential_rejected
            await worker.sync_once()
            with local_factory() as db:
                assert prepare_batch(db, alice_identity)["events"] == []
                db.get(ChatSession, "c").title = "unsent Alice revision"
                db.commit()
            with cloud_factory() as db:
                rows, count = list_records(db, user_id="alice")
                assert count == 1
                assert rows[0]["payload"]["title"] == "retained private conversation"

            await publish(await issue("bob"))
            await worker.sync_once()
            with local_factory() as db:
                assert len(prepare_batch(db, alice_identity)["events"]) == 1
            with cloud_factory() as db:
                assert list_records(db, user_id="bob")[1] == 0
                assert list_records(db, user_id="alice")[0][0]["payload"]["title"] == (
                    "retained private conversation"
                )
            await publish(renewed)
            await worker.sync_once()
            await worker.sync_once()
            with local_factory() as db:
                assert prepare_batch(db, alice_identity)["events"] == []
                assert db.get(ChatSession, "c").title == "unsent Alice revision"
            with cloud_factory() as db:
                rows, count = list_records(db, user_id="alice")
                assert count == 1
                assert rows[0]["payload"]["title"] == "unsent Alice revision"

    try:
        asyncio.run(scenario())
    finally:
        remove()
        local_engine.dispose()
        cloud_engine.dispose()
