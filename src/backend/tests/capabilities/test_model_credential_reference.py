"""Cloud model references are non-secret, account-bound, and refreshed per request."""

import base64
import json
import pytest
import httpx
from core.services import desktop_model_credentials as credentials
from core.services import desktop_cloud_bridge as bridge
from core.capabilities.ref import profile_id
from core.capabilities.errors import CloudUnavailable


def token(subject, device="device-a", nonce="one"):
    claims = {"u": subject, "d": device, "n": nonce}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "dcap2." + payload + ".test-signature"


@pytest.fixture
def state(monkeypatch, caps_root):
    value = {
        "cloud_base": "https://cloud.example",
        "token": token("alice"),
        "expires_at": 9999999999,
        "device_id": "device-a",
    }
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "test-only")
    monkeypatch.setattr(bridge, "get_state", lambda: dict(value))
    return value


def provider(state):
    return {
        "provider_id": "p1",
        "base_url": state["cloud_base"] + "/api/v1/desktop/capability/gateway/models/p1",
        "api_key": state["token"],
    }


def test_import_never_keeps_cloud_bearer(state):
    source = {"providers": [provider(state)]}
    clean = credentials.sanitize_import(source)
    assert state["token"] not in json.dumps(clean)
    assert clean["providers"][0]["api_key"] == "desktop-capability:" + profile_id(
        state["cloud_base"], "alice"
    )
    assert source["providers"][0]["api_key"] == state["token"]


@pytest.mark.asyncio
async def test_transport_rotates_same_account_and_blocks_switch(state):
    p = credentials.sanitize_import({"providers": [provider(state)]})["providers"][0]
    hook, captured = credentials.request_hook(p["api_key"], p["base_url"])
    req = httpx.Request("POST", p["base_url"] + "/chat/completions")
    state["token"] = token("alice", nonce="rotated")
    await hook(req)
    assert req.headers["Authorization"] == "Bearer " + state["token"]
    assert req.headers["X-Desktop-Device-Id"] == "device-a"
    state["token"] = token("bob")
    with pytest.raises(CloudUnavailable):
        await hook(httpx.Request("POST", p["base_url"] + "/chat/completions"))


@pytest.mark.asyncio
async def test_transport_never_sends_bearer_to_other_destination(state):
    p = credentials.sanitize_import({"providers": [provider(state)]})["providers"][0]
    hook, _ = credentials.request_hook(p["api_key"], p["base_url"])
    request = httpx.Request("POST", "https://unrelated.example/chat/completions")
    with pytest.raises(CloudUnavailable):
        await hook(request)
    assert "authorization" not in request.headers


def test_reference_fails_closed_after_logout(state, monkeypatch):
    p = credentials.sanitize_import({"providers": [provider(state)]})["providers"][0]
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    with pytest.raises(CloudUnavailable):
        credentials.headers(p["api_key"], p["base_url"])


def test_cloud_token_cannot_be_imported_as_arbitrary_provider(state):
    p = provider(state)
    p["base_url"] = "https://unrelated.example/v1"
    with pytest.raises(ValueError):
        credentials.sanitize_import({"providers": [p]})
