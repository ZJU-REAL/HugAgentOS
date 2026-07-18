"""Desktop redirect login (approach B): handoff ticket issuance + redemption.

Covers the one-time ticket store's roundtrip and single-use semantics, plus the
end-to-end behavior of the two endpoints /v1/auth/desktop/{handoff,redeem}.
Uses an in-memory session store throughout; no dependency on Redis / DB.
"""

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from core.config import settings as settings_mod


@pytest.fixture
def memory_sessions():
    """Force session / ticket onto in-memory storage (store_type=memory), restore after the test."""
    original = settings_mod.settings.session
    new = dataclasses.replace(original, store_type="memory")
    object.__setattr__(settings_mod.settings, "session", new)
    yield
    object.__setattr__(settings_mod.settings, "session", original)


_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


# ── Ticket store: roundtrip + single use ───────────────────────────────────────────────

def test_ticket_store_roundtrip_and_single_use(memory_sessions):
    from core.auth import desktop_ticket_store as store

    token = _run(store.issue_ticket({"session_token": "raw-tok-1"}))
    assert token

    payload = _run(store.consume_ticket(token))
    assert payload and payload["session_token"] == "raw-tok-1"

    # Single use: the second time must be None
    assert _run(store.consume_ticket(token)) is None


def test_ticket_store_invalid_token(memory_sessions):
    from core.auth import desktop_ticket_store as store

    assert _run(store.consume_ticket("does-not-exist")) is None
    assert _run(store.consume_ticket("")) is None


# ── Endpoints: handoff → redeem end-to-end ───────────────────────────────────────────

@pytest.fixture
def client():
    from api.app import app

    return TestClient(app)


def _make_session_cookie() -> tuple[str, str]:
    """Create a session in the in-memory store, return (cookie_name, raw_token)."""
    from core.auth.session import create_session

    token = _run(create_session({
        "user_id": "u-desktop-1",
        "user_center_id": "uc-1",
        "username": "桌面测试用户",
        "email": None,
    }))
    return settings_mod.settings.session.cookie_name, token


def test_handoff_requires_session(client, memory_sessions):
    # No cookie → 401
    resp = client.post("/v1/auth/desktop/handoff")
    assert resp.status_code == 401


def test_handoff_then_redeem_returns_same_token(client, memory_sessions):
    cookie_name, raw_token = _make_session_cookie()

    # 1. Browser side: exchange the cookie for a handoff ticket
    r1 = client.post("/v1/auth/desktop/handoff", cookies={cookie_name: raw_token})
    assert r1.status_code == 200, r1.text
    handoff = r1.json()["data"]["handoff_ticket"]
    assert handoff

    # 2. App side: exchange the ticket back for the real session token (== the original cookie token)
    r2 = client.post("/v1/auth/desktop/redeem", json={"ticket": handoff})
    assert r2.status_code == 200, r2.text
    data = r2.json()["data"]
    assert data["token"] == raw_token
    assert data["cookie_name"] == cookie_name

    # 3. Single use: redeeming the same ticket again must fail
    r3 = client.post("/v1/auth/desktop/redeem", json={"ticket": handoff})
    assert r3.status_code == 401


def test_redeem_invalid_ticket(client, memory_sessions):
    resp = client.post("/v1/auth/desktop/redeem", json={"ticket": "bogus-ticket"})
    assert resp.status_code == 401
