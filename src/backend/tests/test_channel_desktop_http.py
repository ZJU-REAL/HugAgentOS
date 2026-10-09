"""Desktop client against real cloud routes and their canonical response envelopes."""

import httpx
import pytest
from core.db.models import UserShadow
from core.infra.exceptions import AccessDeniedError, BadRequestError
from core.services import channel_desktop_worker as worker
from tests.test_channel_desktop_relay_api import api, create_local, enqueue  # noqa: F401


@pytest.fixture
def cloud(api, monkeypatch):
    client, factory, _ = api
    state = {"cloud_base": "https://cloud.invalid", "token": "fixture", "device_id": "device-a"}
    monkeypatch.setattr(worker.bridge, "get_state", lambda: state)
    monkeypatch.setattr(worker.bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(worker.bridge, "get_identity_state", lambda: {
        "cloud_base": state["cloud_base"], "subject": "owner", "device_id": "device-a",
        "shell_user_center_id": "center",
    })
    with factory() as db:
        db.get(UserShadow, "owner").user_center_id = "center"
        db.commit()
    from fastapi import FastAPI
    proxy = FastAPI()
    proxy.mount("/api", client.app)
    original = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.ASGITransport(app=proxy), **kw,
    ))
    return state


async def test_desktop_register_claim_renew_reply_complete_accept_cloud_envelopes(api, cloud):
    _, factory, sent = api
    with factory() as db:
        grant = await worker.prepare_binding(db, "owner")
    assert grant["device_name"]
    assert grant["device_id"] == "device-a"

    # A real channel attached to this device is queued and served through the same HTTP client.
    registered, bot = create_local(api)
    delivery = enqueue(factory, bot)
    reply = await worker.cloud_post(cloud, "claim", {"binding_ids": [registered["binding_id"]]})
    assert reply["delivery"]["delivery_id"] == delivery
    lease = reply["delivery"]["lease"]
    assert await worker.cloud_post(cloud, delivery + "/renew", {"lease": lease}) == {"ok": True}
    result = await worker.cloud_post(cloud, delivery + "/operation", {
        "lease": lease, "operation_id": "1", "method": "send_text", "args": {"text": "local reply"},
    })
    assert result["success"]
    attachment = await worker.cloud_post(cloud, delivery + "/operation", {
        "lease": lease, "operation_id": "2", "method": "download_resource",
        "args": {"attachment": {"key": "file-one"}},
    })
    import base64
    assert base64.b64decode(attachment) == b"local-fixture"
    assert sent == [("peer", "local reply")]
    assert await worker.cloud_post(cloud, delivery + "/complete", {"lease": lease}) == {"ok": True}


async def test_cloud_denial_preserves_actionable_reason(api, cloud):
    with pytest.raises(AccessDeniedError, match="本机机器人任务已失效"):
        await worker.cloud_post(cloud, "missing/renew", {"lease": "a" * 32})


@pytest.mark.parametrize("payload", [
    [], {"code": 10000},
])
async def test_malformed_success_does_not_publish_device_grant(api, cloud, monkeypatch, payload):
    # The fixture replaced AsyncClient; use the concrete client class for this transport.
    from httpx import _client
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kw,
    ))
    with pytest.raises(BadRequestError, match="响应格式"):
        await worker.cloud_post(cloud, "register", {"device_name": "PC"})


@pytest.mark.parametrize("payload", [None, [], {}, {"binding_id": "a", "device_id": "other", "device_name": "PC"}])
async def test_invalid_registration_is_not_saved(api, cloud, monkeypatch, payload):
    from httpx import _client
    from core.db.models import ContentBlock
    from sqlalchemy import select

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"code": 10000, "data": payload})), **kw,
    ))
    with api[1]() as db:
        with pytest.raises(BadRequestError, match="响应格式"):
            await worker.prepare_binding(db, "owner")
        assert not db.scalar(select(ContentBlock).where(ContentBlock.id.like(worker.GRANT_PREFIX + "%")))


@pytest.mark.parametrize("channel,transport", [
    ("lark", "long_conn"), ("dingtalk", "long_conn"), ("wecom", "webhook"),
])
async def test_credential_channels_bind_to_registered_desktop(api, cloud, monkeypatch, channel, transport):
    from core.channels.registry import get_adapter
    from core.services.channel_service import ChannelService

    async def validate(self, conn):
        return {}
    monkeypatch.setattr(type(get_adapter(channel)), "validate_credentials", validate)
    monkeypatch.setattr(ChannelService, "_start_long_conn", lambda *args: None)
    client, factory, _ = api
    with factory() as db:
        grant = await worker.prepare_binding(db, "owner")
    response = client.post("/v1/channels/bots", json={
        "channel_type": channel, "app_id": "fixture-app", "app_secret": "fixture-secret",
        "transport": transport, "execution_location": "local", "local_binding_id": grant["binding_id"],
    })
    assert response.status_code == 201
    bot = response.json()["data"]
    assert bot["execution_location"] == "local" and bot["device_id"] == "device-a"
    assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["device_online"] is True


async def test_weixin_qr_local_binding_preserves_selected_device(api, cloud, monkeypatch):
    from core.channels.adapters.weixin import WeixinAdapter
    from core.infra.ephemeral import LocalEphemeralState
    from core.services.channel_service import ChannelService

    async def start():
        return {"qrcode": "fixture-qr", "qrcode_img_content": "fixture-png"}
    async def poll(qrcode):
        assert qrcode == "fixture-qr"
        return {"status": "confirmed", "bot_token": "fixture-weixin-token"}
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)
    monkeypatch.setattr(WeixinAdapter, "start_qr_bind", staticmethod(start))
    monkeypatch.setattr(WeixinAdapter, "poll_qr_status", staticmethod(poll))
    monkeypatch.setattr(ChannelService, "_start_long_conn", lambda *args: None)
    client, factory, _ = api
    with factory() as db:
        grant = await worker.prepare_binding(db, "owner")
    result = client.post("/v1/channels/weixin/bind/start", params={
        "execution_location": "local", "local_binding_id": grant["binding_id"],
    })
    assert result.status_code == 200
    assert result.json()["data"]["qrcode_img"] == "fixture-png"
    bind_id = result.json()["data"]["bind_id"]
    confirmed = client.get(f"/v1/channels/weixin/bind/{bind_id}/status")
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["status"] == "confirmed"
    bot = client.get("/v1/channels/bots").json()["data"]["bots"][0]
    assert bot["execution_location"] == "local" and bot["device_id"] == "device-a"
    assert "fixture-weixin-token" not in str(bot)


async def test_account_switch_rejects_late_registration(api, cloud, monkeypatch):
    from core.capabilities.errors import CloudUnavailable
    from httpx import _client
    active = [cloud]
    monkeypatch.setattr(worker.bridge, "get_state", lambda: active[0])

    def response(request):
        active[0] = {**cloud, "token": "different-account"}
        return httpx.Response(200, json={"code": 10000, "data": {
            "binding_id": "late", "device_id": "device-a", "device_name": "PC",
        }})
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
        transport=httpx.MockTransport(response), **kw,
    ))
    with pytest.raises(CloudUnavailable, match="account changed"):
        await worker.cloud_post(cloud, "register", {"device_name": "PC"})


def test_offline_local_device_is_not_queued_or_routed_to_cloud(api, monkeypatch):
    import time
    from core.db.models import ChannelConnection
    from core.services.channel_relay import ChannelRelayService
    from core.db.models.channel_relay import ChannelRelayDelivery
    from sqlalchemy import select

    grant, bot = create_local(api)
    now = time.time()
    monkeypatch.setattr("core.services.channel_relay.time.time", lambda: now + 36)
    with api[1]() as db:
        assert not ChannelRelayService(db).online(grant["binding_id"])
    with pytest.raises(BadRequestError, match="本机设备离线"):
        enqueue(api[1], bot)
    with api[1]() as db:
        assert db.scalar(select(ChannelRelayDelivery)) is None
        conn = db.get(ChannelConnection, bot["channel_id"])
        assert conn.config["desktop_execution"]["binding_id"] == grant["binding_id"]


async def test_network_failure_returns_retryable_message(api, cloud, monkeypatch):
    from httpx import _client
    def fail(request):
        raise httpx.ConnectError("fixture offline", request=request)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
        transport=httpx.MockTransport(fail), **kw,
    ))
    with pytest.raises(BadRequestError, match="连接失败"):
        await worker.cloud_post(cloud, "register", {"device_name": "PC"})


async def test_other_account_cannot_bind_device_grant_over_http(api, cloud):
    from core.auth.backend import UserContext, get_current_user

    client, factory, _ = api
    with factory() as db:
        grant = await worker.prepare_binding(db, "owner")
        db.add(UserShadow(user_id="other", username="other", extra_data={"can_create_channel_bot": True}))
        db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other-center", username="other",
    )
    response = client.post("/v1/channels/bots", json={
        "channel_type": "lark", "app_id": "other-app", "app_secret": "fixture",
        "transport": "webhook", "execution_location": "local", "local_binding_id": grant["binding_id"],
    })
    assert response.status_code == 400
    assert "本机授权已失效" in response.json()["message"]
    assert client.get("/v1/channels/bots").json()["data"]["bots"] == []
