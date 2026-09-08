"""Real dynamic model objects must keep their captured desktop authorization epoch."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from core.capabilities.errors import CloudUnavailable
from core.capabilities.ref import profile_id
from core.llm import chat_models, hooks
from core.services import desktop_cloud_bridge as bridge
from core.services.model_config import ModelConfigService, ResolvedModelConfig


def state(
    subject="alice",
    *,
    epoch=1,
    session="session-a",
    device="device-a",
    nonce="one",
    cloud="https://cloud.example",
):
    claims = {"u": subject, "c": subject, "a": epoch, "h": session, "d": device, "n": nonce}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "cloud_base": cloud,
        "token": "dcap2." + body + ".synthetic",
        "expires_at": 9999999999,
        "device_id": device,
    }


@pytest_asyncio.fixture
async def models(monkeypatch, caps_root):
    current = {"state": state()}
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "synthetic-shell-secret")
    monkeypatch.setattr(
        bridge, "get_state", lambda: dict(current["state"]) if current["state"] else None
    )
    monkeypatch.setattr(hooks, "_model_cache", {})
    monkeypatch.setattr(hooks, "_cached_version", -1)
    monkeypatch.setattr(chat_models, "_HTTP_CLIENTS", {})
    monkeypatch.setattr("core.llm.context_manager.resolve_model_context_window", lambda model: 8192)
    service = SimpleNamespace(version=0)

    def configure(value, *, direct=False):
        current["state"] = value
        subject = json.loads(base64.urlsafe_b64decode(value["token"].split(".")[1] + "=="))["u"]
        current["config"] = ResolvedModelConfig(
            model_name="synthetic-public-model",
            base_url=value["cloud_base"] + "/api/v1/desktop/capability/gateway/models/p1",
            api_key=(
                "synthetic-direct-key"
                if direct
                else "desktop-capability:" + profile_id(value["cloud_base"], subject)
            ),
            context_length=8192,
            timeout=1,
        )

    configure(current["state"])
    service.resolve = lambda role: current["config"]
    service.resolve_provider = lambda pid: current["config"]
    monkeypatch.setattr(ModelConfigService, "get_instance", lambda: service)
    current.update(configure=configure, service=service)
    yield current
    for client in chat_models._HTTP_CLIENTS.values():
        await client.aclose()


def model(kind, mode="medium"):
    return hooks._get_main_model(mode) if kind == "main" else hooks._get_provider_model("p1", mode)


async def authorize(instance):
    request = httpx.Request("POST", instance.credential.base_url.rstrip("/") + "/chat/completions")
    for hook in instance._http_client.event_hooks["request"]:
        await hook(request)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "provider"])
@pytest.mark.parametrize("change", ["session", "device", "account", "instance"])
async def test_new_desktop_identity_builds_new_model_without_rebinding_old(models, kind, change):
    original = model(kind)
    await authorize(original)
    replacement = {
        "session": state(epoch=2, session="session-b"),
        "device": state(device="device-b"),
        "account": state("bob"),
        "instance": state(cloud="https://cloud.example/tenant-b"),
    }[change]
    models["configure"](replacement)
    fresh = model(kind)
    assert fresh is not original
    assert fresh._http_client is not original._http_client
    request = await authorize(fresh)
    assert request.headers["Authorization"] == "Bearer " + replacement["token"]
    with pytest.raises(CloudUnavailable):
        await authorize(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "provider"])
async def test_same_session_token_refresh_reuses_model_with_current_header(models, kind):
    original = model(kind)
    models["configure"](state(nonce="refreshed"))
    assert model(kind) is original
    request = await authorize(original)
    assert request.headers["Authorization"] == "Bearer " + models["state"]["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "provider"])
async def test_logout_never_returns_cached_cloud_model(models, kind):
    original = model(kind)
    models["state"] = None
    with pytest.raises(CloudUnavailable):
        model(kind)
    with pytest.raises(CloudUnavailable):
        await authorize(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "provider"])
async def test_non_desktop_retains_mode_and_config_version_cache(models, monkeypatch, kind):
    monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET")
    models["configure"](state(), direct=True)

    def forbidden():
        raise AssertionError("non-desktop cache must not consult desktop identity")

    monkeypatch.setattr(bridge, "get_state", forbidden)
    original = model(kind)
    assert model(kind) is original
    assert model(kind, "fast") is not original
    models["service"].version += 1
    assert model(kind) is not original
