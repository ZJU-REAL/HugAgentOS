"""QR login contract against the real Weixin adapter and channel HTTP routes."""

import httpx
import pytest
from core.channels.adapters.weixin import WeixinAdapter
from core.infra.ephemeral import LocalEphemeralState
from core.services.channel_service import ChannelService
from httpx import _client
from tests.test_channel_desktop_relay_api import api, headers  # noqa: F401


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("wait", "waiting"),
        ("scaned", "scanned"),
        ("waiting", "waiting"),
        ("scanned", "scanned"),
        ("confirmed", "confirmed"),
        ("expired", "expired"),
        ("unknown", "unknown"),
    ],
)
async def test_ilink_status_is_normalized_before_desktop_consumes_it(monkeypatch, raw, expected):
    def respond(request):
        assert request.url.path.endswith("/get_qrcode_status")
        return httpx.Response(200, json={"ret": 0, "status": raw})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    assert (await WeixinAdapter.poll_qr_status("fixture"))["status"] == expected


def test_raw_weixin_wait_scan_confirm_creates_local_bot(api, monkeypatch):
    client, _, _ = api
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)
    monkeypatch.setattr(ChannelService, "_start_long_conn", lambda *args: None)
    states = iter(
        [
            {"status": "wait"},
            {"status": "wait"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "fixture-token",
                "baseurl": "https://ilinkai.weixin.qq.com",
            },
        ]
    )

    def respond(request):
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(
                200, json={"qrcode": "fixture-qr", "qrcode_img_content": "fixture-png"}
            )
        assert request.url.path.endswith("/get_qrcode_status")
        return httpx.Response(200, json=next(states))

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    grant = client.post(
        "/v1/channels/desktop/register", headers=headers(), json={"device_name": "Fixture PC"}
    ).json()["data"]
    started = client.post(
        "/v1/channels/weixin/bind/start",
        params={"execution_location": "local", "local_binding_id": grant["binding_id"]},
    )
    assert started.status_code == 200
    bind_id = started.json()["data"]["bind_id"]
    for expected in ["waiting", "waiting", "scanned", "confirmed"]:
        result = client.get(f"/v1/channels/weixin/bind/{bind_id}/status")
        assert result.status_code == 200
        assert result.json()["data"]["status"] == expected
        if expected != "confirmed":
            assert client.get("/v1/channels/bots").json()["data"]["bots"] == []
    bot = client.get("/v1/channels/bots").json()["data"]["bots"][0]
    assert bot["execution_location"] == "local"
    assert bot["device_id"] == "device-a" and bot["device_online"]
    assert bot["channel_id"] == result.json()["data"]["channel_id"]
    assert "fixture-token" not in str(bot)


async def test_poll_timeout_keeps_waiting(monkeypatch):
    def respond(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    assert await WeixinAdapter.poll_qr_status("fixture") == {"status": "waiting"}


@pytest.mark.parametrize("token", [None, "", "   ", 123])
def test_confirmation_without_credentials_does_not_report_success(api, monkeypatch, token):
    client, _, _ = api
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)

    def respond(request):
        if request.url.path.endswith("/get_bot_qrcode"):
            data = {"qrcode": "fixture-qr", "qrcode_img_content": "fixture-png"}
        else:
            data = {"status": "confirmed", "bot_token": token}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    bind_id = client.post("/v1/channels/weixin/bind/start").json()["data"]["bind_id"]
    status = client.get(f"/v1/channels/weixin/bind/{bind_id}/status")
    assert status.status_code == 400
    assert "凭据" in status.json()["message"]
    assert client.get("/v1/channels/bots").json()["data"]["bots"] == []


@pytest.mark.parametrize("status", ["expired", "unknown"])
def test_terminal_qr_states_do_not_create_bot(api, monkeypatch, status):
    client, _, _ = api
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)

    def respond(request):
        data = (
            {"qrcode": "fixture-qr", "qrcode_img_content": "fixture-png"}
            if request.url.path.endswith("/get_bot_qrcode")
            else {"status": status}
        )
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    bind_id = client.post("/v1/channels/weixin/bind/start").json()["data"]["bind_id"]
    result = client.get(f"/v1/channels/weixin/bind/{bind_id}/status")
    assert result.json()["data"]["status"] == status
    assert client.get("/v1/channels/bots").json()["data"]["bots"] == []
