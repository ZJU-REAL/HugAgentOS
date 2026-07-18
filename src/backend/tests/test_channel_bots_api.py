"""Channel bots HTTP end-to-end integration tests (TestClient + dependency overrides).

Covers the real route -> service -> adapter chain of all /v1/channels/* endpoints:
adapters list, capability-bit gate (403), CRUD, token lock (400), credentials not leaked,
enable/disable, webhook url_verification challenge, webhook signature verification failure (403).

Credential validation (validate_credentials) and long-connection startup touch the network /
spawn threads, so they are uniformly monkeypatched away.
"""

import base64
import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, get_db
from core.db.models import UserShadow


@pytest.fixture
def client(monkeypatch):
    # 1) Single-connection shared in-memory DB (TestClient still sees the same tables across threads)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    seed = Session()
    seed.add(UserShadow(user_id="u1", username="alice", extra_data={"can_create_channel_bot": True}))
    seed.add(UserShadow(user_id="u_deny", username="bob", extra_data={}))
    seed.commit(); seed.close()

    # 2) Monkeypatch away side effects that touch the network / spawn threads
    async def _fake_validate(self, conn):
        return {"app_id": conn.app_id}
    monkeypatch.setattr("core.channels.adapters.lark.LarkAdapter.validate_credentials", _fake_validate)
    monkeypatch.setattr("core.channels.manager.ChannelManager.start_connection", lambda self, *a, **k: None)
    monkeypatch.setattr("core.channels.manager.ChannelManager.stop_connection", lambda self, *a, **k: None)

    from api.app import app

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    state = {"uid": "u1"}
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=state["uid"], user_center_id="c", username=state["uid"],
    )
    app.dependency_overrides[get_db] = _override_db
    c = TestClient(app)
    c.uidref = state  # lets tests switch the current user
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def _webhook_bot(client) -> str:
    """Create a webhook-mode bot and return its channel_id."""
    r = client.post("/v1/channels/bots", json={
        "channel_type": "lark", "app_id": "cli_wh", "app_secret": "sec",
        "transport": "webhook", "encrypt_key": "ekey", "display_name": "群助手",
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["channel_id"]


def test_adapters_list(client):
    r = client.get("/v1/channels/adapters")
    assert r.status_code == 200
    types = [a["channel_type"] for a in r.json()["data"]["adapters"]]
    assert "lark" in types


def test_capability_gate_denied(client):
    client.uidref["uid"] = "u_deny"
    r = client.post("/v1/channels/bots", json={
        "channel_type": "lark", "app_id": "cli_x", "app_secret": "s",
    })
    assert r.status_code == 403, r.text


def test_create_list_hides_secret_and_token_lock(client):
    # Create (webhook, to avoid long-connection threads)
    r = client.post("/v1/channels/bots", json={
        "channel_type": "lark", "app_id": "cli_a", "app_secret": "TOPSECRET",
        "transport": "webhook", "display_name": "我的bot",
    })
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["status"] == "connected"
    assert data["webhook_path"].endswith("/webhook")
    # List does not leak the secret
    r = client.get("/v1/channels/bots")
    assert r.status_code == 200
    bots = r.json()["data"]["bots"]
    assert len(bots) == 1
    assert "TOPSECRET" not in json.dumps(bots)
    assert "config" not in bots[0]
    # token lock: binding the same app_id again -> 400
    r = client.post("/v1/channels/bots", json={
        "channel_type": "lark", "app_id": "cli_a", "app_secret": "s2", "transport": "webhook",
    })
    assert r.status_code == 400, r.text


def test_update_test_delete_lifecycle(client):
    cid = _webhook_bot(client)
    # Disable
    r = client.patch(f"/v1/channels/bots/{cid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False
    # Resource allowlist
    r = client.patch(f"/v1/channels/bots/{cid}", json={"resource_scope": {"kb_ids": ["k1"], "skill_ids": ["s1"]}})
    assert r.json()["data"]["resource_scope"] == {"kb_ids": ["k1"], "skill_ids": ["s1"]}
    # Test credentials (validate is already mocked)
    r = client.post(f"/v1/channels/bots/{cid}/test")
    assert r.status_code == 200 and r.json()["data"]["ok"] is True
    # Delete
    r = client.delete(f"/v1/channels/bots/{cid}")
    assert r.status_code == 200
    assert client.get("/v1/channels/bots").json()["data"]["bots"] == []


def test_cannot_touch_others_bot(client):
    cid = _webhook_bot(client)              # created by u1
    client.uidref["uid"] = "u_deny"          # switch to a different user
    r = client.delete(f"/v1/channels/bots/{cid}")
    assert r.status_code == 403, r.text


def test_webhook_url_verification(client):
    cid = _webhook_bot(client)
    r = client.post(f"/v1/channels/{cid}/webhook", json={"type": "url_verification", "challenge": "CHAL123"})
    assert r.status_code == 200
    assert r.json() == {"challenge": "CHAL123"}


def test_webhook_bad_signature_rejected(client):
    cid = _webhook_bot(client)              # configured with encrypt_key="ekey"
    # Build an encrypted message event but supply a wrong signature -> verification fails 403
    payload = {"header": {"event_type": "im.message.receive_v1"}, "event": {}}
    plain = json.dumps(payload).encode()
    key = hashlib.sha256(b"ekey").digest()
    iv = b"\x00" * 16
    pad = 16 - (len(plain) % 16)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(plain + bytes([pad]) * pad) + enc.finalize()
    body = {"encrypt": base64.b64encode(iv + ct).decode()}
    r = client.post(
        f"/v1/channels/{cid}/webhook",
        content=json.dumps(body),
        headers={
            "x-lark-signature": "deadbeef",   # wrong signature
            "x-lark-request-timestamp": "1",
            "x-lark-request-nonce": "n",
        },
    )
    assert r.status_code == 403, r.text
