"""Delivery failures keep the durable outbox and expose only safe diagnostics."""
from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity

import asyncio
import base64
import json
import logging
import time

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.models import ChatSession, UserShadow
from features.desktop.models import DesktopOutbox, DesktopRecord, DesktopSyncDevice
from features.desktop.bridge import client as bridge
from features.desktop.observability.sync import SOURCES, DesktopSyncWorker, install_capture, prepare_batch


def token(user="alice", nonce="initial"):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(dict(u=user, c=user, d="d", a=1, h="session", nonce=nonce)).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"dcap2.{payload}.signature"


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "local.db"))
    for model in {UserShadow, DesktopOutbox, *(m for m, _ in SOURCES.values())}:
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    state = dict(
        cloud_base="http://cloud", token=token(), device_id="d", expires_at=time.time() + 3600
    )
    identity = dict(
        cloud_base="http://cloud",
        subject="alice",
        device_id="d",
        shell_user_center_id="cloud:alice",
    )
    monkeypatch.setattr(bridge_client_state, 'state', state)
    monkeypatch.setattr(bridge_client_state, 'state_loaded', True)
    monkeypatch.setattr(bridge_client_state, 'credential_rejected', "")
    monkeypatch.setattr(bridge_identity, 'get_identity_state', lambda: identity)
    remove = install_capture(factory, lambda: identity)
    with factory() as db:
        db.add(UserShadow(user_id="local", username="Alice", user_center_id="cloud:alice"))
        db.add(ChatSession(chat_id="c", user_id="local", title="private-business-content"))
        db.commit()
    yield factory, identity, state
    remove()
    engine.dispose()


@pytest.mark.parametrize("failure", [401, 403, 413, 429, 503, "read", "connect", "decode"])
def test_failed_delivery_retains_outbox_and_safe_diagnostics(delivery, caplog, failure):
    factory, identity, state = delivery
    trace_ids = []

    async def fail(request):
        trace_ids.append(request.headers["x-trace-id"])
        if failure == "read":
            raise httpx.ReadTimeout("credential-secret private-business-content", request=request)
        if failure == "connect":
            raise httpx.ConnectError("credential-secret private-business-content", request=request)
        if failure == "decode":
            return httpx.Response(200, content="credential-secret private-business-content")
        return httpx.Response(
            failure,
            headers={"x-trace-id": "credential-secret"},
            json={"detail": "credential-secret private-business-content"},
        )

    caplog.set_level(logging.WARNING)
    worker = DesktopSyncWorker(factory, transport=httpx.MockTransport(fail))
    with pytest.raises((httpx.HTTPError, ValueError)):
        asyncio.run(worker.sync_once())
    with factory() as db:
        assert len(prepare_batch(db, identity)["events"]) == 1
    assert bool(bridge_client_state.credential_rejected) == (failure == 401)
    assert "elapsed_ms=" in caplog.text
    assert "batch_bytes=" in caplog.text and "event_count=1" in caplog.text
    assert trace_ids[0] in caplog.text
    assert "credential-secret" not in caplog.text
    assert "private-business-content" not in caplog.text
    if isinstance(failure, int):
        assert f"http_status={failure}" in caplog.text
    if failure == "read":
        assert "error_type=ReadTimeout" in caplog.text


def test_401_renewal_retries_to_real_cloud_endpoint_without_losing_records(
    delivery, tmp_path, monkeypatch
):
    from features.desktop.api import desktop_observability as route
    from features.desktop.observability.projection import list_records
    from fastapi import FastAPI

    factory, identity, state = delivery
    engine = create_engine("sqlite:///" + str(tmp_path / "cloud.db"))
    for model in (DesktopRecord, DesktopSyncDevice, UserShadow):
        model.__table__.create(engine)
    cloud_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(route, "SessionLocal", cloud_factory)
    app = FastAPI()
    app.include_router(route.router, prefix="/api")
    app.dependency_overrides[route._require_capability_user] = lambda: "alice"
    real_transport = httpx.ASGITransport(app=app)
    seen = []

    async def endpoint(request):
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return await real_transport.handle_async_request(request)

    worker = DesktopSyncWorker(factory, transport=httpx.MockTransport(endpoint))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(worker.sync_once())
    assert bridge_client_state.credential_rejected
    monkeypatch.setattr(bridge_identity, 'purge_persisted_state', lambda: None)
    monkeypatch.setattr(bridge_identity, 'rebuild_identity_views', lambda: None)
    monkeypatch.setattr(bridge_identity, 'clear_partial_selection', lambda: None)
    refreshed = []
    monkeypatch.setattr(bridge_client, 'refresh_manifest_async', lambda **kw: refreshed.append(kw))
    renewed = token(nonce="renewed")
    bridge_client.set_state("http://cloud", renewed, 3600, device_id="d")
    assert not bridge_client_state.credential_rejected
    assert refreshed == [{"force": True}]
    asyncio.run(worker.sync_once())
    asyncio.run(worker.sync_once())  # Cloud accepts the duplicate heartbeat idempotently.
    assert seen == [f"Bearer {state['token']}", f"Bearer {renewed}", f"Bearer {renewed}"]
    with factory() as db:
        assert prepare_batch(db, identity)["events"] == []
        assert db.get(ChatSession, "c").title == "private-business-content"
    with cloud_factory() as db:
        records, count = list_records(db, user_id="alice")
        assert count == 1
        assert records[0]["payload"]["title"] == "private-business-content"
    engine.dispose()


@pytest.mark.parametrize("change", ["rotation", "account", "logout"])
def test_late_401_never_rejects_replacement_credentials(delivery, monkeypatch, change):
    factory, _, state = delivery

    async def late_reply(request):
        replacement = {**state, "token": token(nonce="replacement")}
        if change == "account":
            replacement["token"] = token(user="bob")
        monkeypatch.setattr(bridge_client_state, 'state', None if change == "logout" else replacement)
        return httpx.Response(401)

    worker = DesktopSyncWorker(factory, transport=httpx.MockTransport(late_reply))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(worker.sync_once())
    assert not bridge_client_state.credential_rejected


def test_refusal_uses_actual_request_token_when_headers_renewed_snapshot(delivery, monkeypatch):
    _, _, snapshot = delivery
    renewed = {**snapshot, "token": token(nonce="new")}
    monkeypatch.setattr(bridge_client_state, 'state', renewed)
    request = httpx.Request(
        "POST",
        "http://cloud/api/v1/desktop/observability/batch",
        headers=bridge_identity.cloud_headers(snapshot),
    )
    refusal = httpx.HTTPStatusError(
        "Unauthorized", request=request, response=httpx.Response(401, request=request)
    )
    bridge_identity.note_rejected_credential(snapshot, refusal)
    assert bridge_client_state.credential_rejected == bridge_identity.state_fingerprint(renewed)


@pytest.mark.parametrize("authorization", [None, "Bearer different-token"])
def test_missing_or_unrelated_request_credential_cannot_mark_rejection(delivery, authorization):
    _, _, snapshot = delivery
    request = httpx.Request("POST", "http://cloud")
    if authorization:
        request.headers["Authorization"] = authorization
    exc = httpx.HTTPStatusError(
        "Unauthorized", request=request, response=httpx.Response(401, request=request)
    )
    bridge_identity.note_rejected_credential(snapshot, exc)
    assert not bridge_client_state.credential_rejected


def test_other_account_snapshot_cannot_reject_live_request_token(delivery):
    _, _, state = delivery
    other = {**state, "token": token(user="bob")}
    request = httpx.Request("POST", "http://cloud", headers=bridge_identity.cloud_headers(state))
    exc = httpx.HTTPStatusError(
        "Unauthorized", request=request, response=httpx.Response(401, request=request)
    )
    bridge_identity.note_rejected_credential(other, exc)
    assert not bridge_client_state.credential_rejected
