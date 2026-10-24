"""Bearer tokens are verified off the event loop and once per token per window."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException

from core.auth import backend, bearer
from core.config.settings import settings
from core.db.models import UserShadow
from core.infra.ephemeral import LocalEphemeralState
from tests.db_support import share_session

_AsyncClient = httpx.AsyncClient


def _serve(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        bearer.httpx, "AsyncClient", lambda **kwargs: _AsyncClient(transport=transport, **kwargs)
    )


@pytest.fixture
def auth_settings():
    """Swap the frozen auth settings for one test."""
    original = settings.auth

    def apply(**changes):
        object.__setattr__(settings, "auth", replace(original, **changes))

    yield apply
    object.__setattr__(settings, "auth", original)


@pytest.fixture
def user_center(monkeypatch, auth_settings):
    """A remote-mode user center that accepts ``good`` and counts its requests."""
    calls = []

    def respond(request):
        calls.append(request.headers["authorization"])
        token = request.headers["authorization"]
        if request.url.path == "/verify" and token == "Bearer moved":
            return httpx.Response(307, headers={"location": "http://uc/v2/verify"})
        if token in ("Bearer good", "Bearer moved"):
            return httpx.Response(
                200, json={"user_center_id": "uc-1", "username": "alice", "secret": "s"}
            )
        if token == "Bearer nobody":
            return httpx.Response(200, json={})
        return httpx.Response(401)

    _serve(monkeypatch, respond)
    auth_settings(mode="remote", api_url="http://uc", retry_count=2)
    state = LocalEphemeralState()
    monkeypatch.setattr(bearer, "get_ephemeral_state", lambda: state)
    return calls


@pytest.fixture
def login_audits(monkeypatch):
    """Login audit actions written through the backend, in order (edition independent)."""
    actions = []

    class Recorder:
        def __init__(self, db):
            pass

        def create(self, entry):
            # audit_logs only accepts these statuses, and user_id must name a real user.
            assert entry["status"] in ("success", "failure", "error")
            assert entry["user_id"] is None or entry["user_id"].startswith("user_")
            actions.append(entry["action"])

    monkeypatch.setattr(backend, "AuditLogRepository", Recorder)
    return actions


def test_verified_token_is_reused_without_asking_again(user_center):
    first = asyncio.run(bearer.verify_bearer("good"))
    second = asyncio.run(bearer.verify_bearer("good"))

    alice = {"user_center_id": "uc-1", "username": "alice", "email": None, "avatar_url": None}
    assert first == (alice, True)
    assert second == (alice, False)
    assert user_center == ["Bearer good"]


def test_only_the_identity_fields_reach_the_shared_store(user_center):
    asyncio.run(bearer.verify_bearer("good"))
    state = bearer.get_ephemeral_state()
    (key,) = asyncio.run(state.keys("auth:bearer:"))
    assert "good" not in key
    assert "secret" not in asyncio.run(state.get(key))


def test_redirected_verify_endpoint_is_followed(user_center):
    identity, checked = asyncio.run(bearer.verify_bearer("moved"))
    assert identity["user_center_id"] == "uc-1" and checked
    assert user_center == ["Bearer moved", "Bearer moved"]


def test_reply_naming_nobody_is_a_rejection(user_center):
    assert asyncio.run(bearer.verify_bearer("nobody")) == (None, True)
    assert asyncio.run(bearer.verify_bearer("nobody")) == (None, True)


def test_rejected_token_is_not_remembered(user_center):
    assert asyncio.run(bearer.verify_bearer("bad")) == (None, True)
    assert asyncio.run(bearer.verify_bearer("bad")) == (None, True)
    assert user_center == ["Bearer bad", "Bearer bad"]


def test_unreachable_user_center_is_a_502(monkeypatch, user_center):
    def fail(request):
        raise httpx.ConnectError("down", request=request)

    _serve(monkeypatch, fail)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(bearer.verify_bearer("good"))
    assert caught.value.status_code == 502


def test_login_is_audited_once_per_verification(db_session, user_center, login_audits, monkeypatch):
    share_session(monkeypatch, db_session)
    for _ in range(3):
        user = asyncio.run(backend._resolve_bearer_user("good", audit=True))
    assert user.username == "alice"
    assert db_session.query(UserShadow).filter_by(user_center_id="uc-1").count() == 1
    assert login_audits == ["auth.login.success"]

    with pytest.raises(HTTPException) as caught:
        asyncio.run(backend._resolve_bearer_user("bad", audit=True))
    assert caught.value.status_code == 401
    assert login_audits == ["auth.login.success", "auth.login.failed"]


def test_mock_mode_never_calls_out_or_audits(db_session, auth_settings, login_audits, monkeypatch):
    share_session(monkeypatch, db_session)
    auth_settings(mode="mock")
    user = asyncio.run(backend._resolve_bearer_user("bob", audit=True))
    assert user.user_center_id == "bob"
    assert login_audits == []


def test_concurrent_requests_share_one_verification(user_center):
    async def burst():
        return await asyncio.gather(*[bearer.verify_bearer("good") for _ in range(10)])

    results = asyncio.run(burst())
    assert user_center == ["Bearer good"]
    assert [checked for _, checked in results].count(True) == 1
    assert all(identity["user_center_id"] == "uc-1" for identity, _ in results)


def test_cancelled_first_caller_does_not_cancel_the_shared_verification(monkeypatch, user_center):
    release = asyncio.Event()
    ask = bearer._ask_user_center

    async def slow_ask(token):
        await release.wait()
        return await ask(token)

    monkeypatch.setattr(bearer, "_ask_user_center", slow_ask)

    async def scenario():
        first = asyncio.create_task(bearer.verify_bearer("good"))
        await asyncio.sleep(0)
        second = asyncio.create_task(bearer.verify_bearer("good"))
        await asyncio.sleep(0)
        first.cancel()
        release.set()
        return await second

    identity, checked = asyncio.run(scenario())
    assert identity["user_center_id"] == "uc-1" and checked is False
    assert user_center == ["Bearer good"]
