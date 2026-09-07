"""Desktop token contract; synthetic memory sessions and a fixed fake signing key only."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import session
from core.config.settings import settings
from core.services import desktop_capability as cap
from api.routes.v1 import desktop_capability as routes
from api.deps import require_config

ISSUER = "https://cloud.example"
DEVICE = "synthetic-device-a"


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch):
    monkeypatch.setattr(session, "_MEMORY_SESSIONS", {})
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    monkeypatch.setattr(cap, "_secret_cache", "ab" * 32)
    monkeypatch.setattr(cap, "_known_cloud_secrets", lambda uid: set())


def _session(uid="user-a"):
    return asyncio.run(
        session.create_session(
            {"user_id": uid, "user_center_id": "center-" + uid}, ttl_seconds=3600
        )
    )


def _issue(cookie, uid="user-a", **kwargs):
    session_hash = session._hash_token(cookie)
    payload = session._MEMORY_SESSIONS[session_hash]["payload"]
    return cap.issue_capability_token(
        uid,
        device_id=DEVICE,
        issuer=ISSUER,
        session_hash=session_hash,
        authorization_epoch=cap.session_authorization_epoch(payload),
        user_center_id=payload["user_center_id"],
        **kwargs,
    )


def _verify(token, *, device=DEVICE, issuer=ISSUER):
    return asyncio.run(cap.verify_capability_token(token, device_id=device, issuer=issuer))


def _resign(token, **changes):
    prefix, body, _ = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload.update(changes)
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{prefix}.{body}.{cap._sign(body.encode())}"


def test_short_token_roundtrip_has_device_session_epoch():
    cookie = _session()
    data = _issue(cookie)
    assert data["token"].startswith("dcap2.")
    assert data["expires_in"] == 600
    assert data["device_id"] == DEVICE
    assert data["authorization_epoch"] > 0
    assert _verify(data["token"]) == "user-a"
    assert cookie not in data["token"]


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "wrong-audience"},
        {"iss": "https://other.example"},
        {"s": "other_scope"},
        {"d": "another-device"},
        {"a": 1},
        {"h": "0" * 64},
        {"u": "different-user"},
        {"iat": 2**53},
        {"e": 1},
        {"e": 2**53},
        {"a": True},
        {"c": "different-center"},
        {"c": ""},
    ],
)
def test_wrong_signed_claims_are_rejected(change):
    token = _issue(_session())["token"]
    assert _verify(_resign(token, **change)) is None


def test_wrong_or_missing_device_and_wrong_issuer_rejected():
    token = _issue(_session())["token"]
    assert _verify(token, device="device-b") is None
    assert _verify(token, device="") is None
    assert _verify(token, issuer="https://other.example") is None


def test_old_prefix_and_tampering_rejected():
    token = _issue(_session())["token"]
    assert _verify(token.replace("dcap2.", "dcap1.", 1)) is None
    assert _verify(token + "x") is None
    assert _verify("garbage") is None


def test_token_expiry_and_long_ttl_capped(monkeypatch):
    token = _issue(_session(), ttl_s=86400)
    assert token["expires_in"] == 600
    now = time.time()
    monkeypatch.setattr(cap.time, "time", lambda: now + 601)
    assert _verify(token["token"]) is None


def test_logout_immediately_revokes_derived_token():
    cookie = _session()
    token = _issue(cookie)["token"]
    assert _verify(token) == "user-a"
    assert asyncio.run(session.revoke_session(cookie))
    assert _verify(token) is None
    _session()  # Same subject signing in again cannot revive the previous epoch.
    assert _verify(token) is None


def test_expired_session_and_changed_epoch_are_rejected():
    cookie = _session()
    token = _issue(cookie)["token"]
    entry = session._MEMORY_SESSIONS[session._hash_token(cookie)]
    entry["payload"]["created_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    assert _verify(token) is None
    entry["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert _verify(token) is None


def test_token_check_does_not_renew_session():
    cookie = _session()
    token = _issue(cookie)["token"]
    entry = session._MEMORY_SESSIONS[session._hash_token(cookie)]
    expiry = entry["expires_at"]
    assert _verify(token) == "user-a"
    assert entry["expires_at"] == expiry


def _client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app, base_url=ISSUER)


def test_issue_requires_actual_session_and_device():
    with _client() as client:
        missing = client.post("/v1/desktop/capability/token", json={"device_id": DEVICE})
        assert missing.status_code == 401
        cookie = _session()
        client.cookies.set(settings.session.cookie_name, cookie)
        assert client.post("/v1/desktop/capability/token", json={}).status_code == 422
        response = client.post("/v1/desktop/capability/token", json={"device_id": DEVICE})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["expires_in"] == 600
        assert _verify(data["token"]) == "user-a"
        assert response.headers["cache-control"] == "no-store"


def test_all_capability_endpoints_reject_wrong_device_before_resolver(monkeypatch):
    cookie = _session()
    token = _issue(cookie)["token"]
    called = []
    monkeypatch.setattr(cap, "build_user_capability_manifest", lambda uid: called.append(uid))
    with _client() as client:
        response = client.get(
            "/v1/desktop/capability/manifest",
            headers={
                "Authorization": "Bearer " + token,
                "X-Desktop-Device-Id": "other-device",
            },
        )
    assert response.status_code == 401
    assert not called


def test_online_session_store_failure_is_closed(monkeypatch):
    token = _issue(_session())["token"]

    async def unavailable(_digest):
        raise RuntimeError("synthetic session store unavailable")

    monkeypatch.setattr(session, "find_session_by_hash", unavailable)
    assert _verify(token) is None


def test_issuer_config_supports_proxy_aliases_without_trusting_headers(monkeypatch):
    monkeypatch.setenv("DESKTOP_CAPABILITY_ISSUER", "https://Cloud.Example:443/tenant-a/")
    token = _issue(_session())["token"]
    assert cap.capability_issuer("http://internal:3001") == "https://cloud.example/tenant-a"
    assert _verify(token, issuer="http://internal:3001") == "user-a"
    monkeypatch.setenv("DESKTOP_CAPABILITY_ISSUER", "https://cloud.example/tenant-b")
    assert _verify(token, issuer="http://internal:3001") is None


def test_issuer_keeps_scheme_and_tenant_path_separate(monkeypatch):
    monkeypatch.delenv("DESKTOP_CAPABILITY_ISSUER", raising=False)
    assert (
        cap.capability_issuer("https://Cloud.Example:443/tenant-a/")
        == "https://cloud.example/tenant-a"
    )
    assert cap.capability_issuer("http://cloud.example/tenant-a") != cap.capability_issuer(
        "https://cloud.example/tenant-a"
    )
    assert cap.capability_issuer("https://cloud.example/tenant-a") != cap.capability_issuer(
        "https://cloud.example/tenant-b"
    )


def test_mock_bearer_and_session_without_stable_subject_cannot_issue():
    with _client() as client:
        response = client.post(
            "/v1/desktop/capability/token",
            json={"device_id": DEVICE},
            headers={"Authorization": "Bearer mock-user"},
        )
        assert response.status_code == 401
        cookie = asyncio.run(session.create_session({"user_id": "legacy-no-center"}))
        client.cookies.set(settings.session.cookie_name, cookie)
        assert (
            client.post("/v1/desktop/capability/token", json={"device_id": DEVICE}).status_code
            == 401
        )


async def test_redis_session_online_revocation_and_no_renewal(monkeypatch):
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(session, "_use_memory_store", lambda: False)
    monkeypatch.setattr(session, "get_redis", lambda: fake)
    cookie = await session.create_session(
        {"user_id": "redis-user", "user_center_id": "redis-center"}, ttl_seconds=300
    )
    digest = session._hash_token(cookie)
    payload = await session.find_session_by_hash(digest)
    token = cap.issue_capability_token(
        "redis-user",
        device_id=DEVICE,
        issuer=ISSUER,
        session_hash=digest,
        authorization_epoch=cap.session_authorization_epoch(payload),
        user_center_id="redis-center",
    )["token"]
    key = session.SESSION_KEY_PREFIX + digest
    before = await fake.pttl(key)
    assert await cap.verify_capability_token(token, device_id=DEVICE, issuer=ISSUER) == "redis-user"
    assert await fake.pttl(key) <= before
    await session.revoke_session(cookie)
    assert await cap.verify_capability_token(token, device_id=DEVICE, issuer=ISSUER) is None
    await fake.aclose()


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-secret"},
        {"Authorization": "Bearer synthetic-shell-secret", "Origin": "https://evil.example"},
    ],
)
def test_bridge_control_requires_shell_secret_even_if_config_permission_is_granted(
    monkeypatch, method, headers
):
    from core.services import desktop_cloud_bridge as bridge

    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "synthetic-shell-secret")
    calls = []
    monkeypatch.setattr(bridge, "set_state", lambda *a, **kw: calls.append("set"))
    monkeypatch.setattr(bridge, "clear_state", lambda: calls.append("clear"))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_config] = lambda: None
    with TestClient(app) as client:
        response = client.request(
            method,
            "/v1/desktop/capability/cloud-bridge",
            headers=headers,
            json={
                "cloud_base": ISSUER,
                "token": "synthetic-token",
                "device_id": DEVICE,
            },
        )
    assert response.status_code in (401, 403)
    assert calls == []


def test_bridge_control_accepts_direct_shell_for_set_and_clear(monkeypatch):
    from core.services import desktop_cloud_bridge as bridge

    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "synthetic-shell-secret")
    calls = []
    monkeypatch.setattr(
        bridge, "set_state", lambda *a, **kw: calls.append(("set", kw["device_id"]))
    )
    monkeypatch.setattr(bridge, "clear_state", lambda: calls.append(("clear", None)))
    with _client() as client:
        headers = {"Authorization": "Bearer synthetic-shell-secret"}
        response = client.post(
            "/v1/desktop/capability/cloud-bridge",
            headers=headers,
            json={
                "cloud_base": ISSUER,
                "token": "synthetic-token",
                "device_id": DEVICE,
            },
        )
        assert response.status_code == 200
        assert (
            client.delete("/v1/desktop/capability/cloud-bridge", headers=headers).status_code == 200
        )
    assert calls == [("set", DEVICE), ("clear", None)]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/manifest", None),
        ("GET", "/models", None),
        ("GET", "/skills/manifest", None),
        ("GET", "/skills/example/bundle", None),
        ("GET", "/agents/manifest", None),
        ("GET", "/agents/example/bundle", None),
        ("GET", "/plugins/manifest", None),
        ("GET", "/plugins/example/bundle", None),
        ("POST", "/gateway/example/mcp", {}),
        (
            "POST",
            "/gateway/example/call",
            {"tool_name": "search", "arguments": {}, "schema_hash": "a" * 64},
        ),
        ("POST", "/gateway/models/example/chat/completions", {"messages": []}),
    ],
)
def test_every_gateway_and_download_checks_live_session(method, path, body):
    cookie = _session()
    token = _issue(cookie)["token"]
    asyncio.run(session.revoke_session(cookie))
    with _client() as client:
        response = client.request(
            method,
            "/v1/desktop/capability" + path,
            json=body,
            headers={
                "Authorization": "Bearer " + token,
                "X-Desktop-Device-Id": DEVICE,
            },
        )
    assert response.status_code == 401


def test_next_dispatch_checkpoint_stops_after_logout(monkeypatch):
    cookie = _session()
    token = _issue(cookie)["token"]
    resolved = []

    def manifest(uid):
        resolved.append(uid)
        return {"revision": "a" * 64, "servers": []}

    monkeypatch.setattr(cap, "build_user_capability_manifest", manifest)
    headers = {"Authorization": "Bearer " + token, "X-Desktop-Device-Id": DEVICE}
    with _client() as client:
        assert client.get("/v1/desktop/capability/manifest", headers=headers).status_code == 200
        asyncio.run(session.revoke_session(cookie))
        assert client.get("/v1/desktop/capability/manifest", headers=headers).status_code == 401
    assert resolved == ["user-a"]


def test_non_ascii_invalid_control_header_is_a_denial(monkeypatch):
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "synthetic-shell-secret")
    assert not cap.is_desktop_shell_control("Bearer é", None)
