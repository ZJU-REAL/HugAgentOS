"""WeChat HTTP business errors must not pass credential validation."""

from types import SimpleNamespace

import httpx
import pytest
from integrations.channels.adapters.weixin import WeixinAdapter
from core.infra.crypto import encrypt_secret
from httpx import _client
from tests.integrations.channels.test_channel_desktop_relay_api import api  # noqa: F401


@pytest.mark.parametrize(
    "body",
    [
        {"ret": -14, "err_msg": "SENSITIVE_UPSTREAM_DETAIL"},
        {"ret": 0, "errcode": 401, "errmsg": "SENSITIVE_UPSTREAM_DETAIL"},
        {"ret": "invalid"},
        [],
    ],
)
async def test_business_failure_cannot_validate_credentials(monkeypatch, body):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw
        ),
    )
    conn = SimpleNamespace(app_id="fixture", config={"bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat"})
    with pytest.raises(RuntimeError) as error:
        await WeixinAdapter().validate_credentials(conn)
    assert "SENSITIVE_UPSTREAM_DETAIL" not in str(error.value)


@pytest.mark.parametrize("body", [{"ret": -14}, {"ret": 0, "errcode": 401}])
def test_poll_business_error_never_dispatches_message(monkeypatch, body):
    body = {
        **body,
        "msgs": [
            {
                "message_type": 1,
                "from_user_id": "peer",
                "context_token": "ctx",
                "item_list": [{"text_item": {"text": "must not deliver"}}],
            }
        ],
    }
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _client.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw
        ),
    )
    conn = SimpleNamespace(
        channel_id="fixture", config={"bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat"}
    )
    received = []
    poller = WeixinAdapter().make_ws_client(conn, received.append)
    with pytest.raises(RuntimeError):
        poller.start()
    assert received == []


@pytest.mark.parametrize(
    "status,body",
    [
        (200, {"ret": 0, "errcode": 0}),
        (200, {"ret": "0"}),
        (403, {"ret": 0}),
        (200, None),
    ],
)
async def test_credentials_transport_and_success_contract(monkeypatch, status, body):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (
                    httpx.Response(status, json=body)
                    if body is not None
                    else httpx.Response(status, text="not-json")
                )
            ),
            **kw,
        ),
    )
    conn = SimpleNamespace(app_id="fixture", config={"bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat"})
    if status == 200 and body is not None:
        assert await WeixinAdapter().validate_credentials(conn) == {"app_id": "fixture"}
    else:
        with pytest.raises(RuntimeError):
            await WeixinAdapter().validate_credentials(conn)


def test_poll_network_timeout_is_not_silently_connected(monkeypatch):
    def respond(request):
        raise httpx.ReadTimeout("fixture")

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _client.Client(transport=httpx.MockTransport(respond), **kw)
    )
    conn = SimpleNamespace(
        channel_id="fixture", config={"bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat"}
    )
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        WeixinAdapter().make_ws_client(conn, lambda msg: None).start()


@pytest.mark.parametrize(
    "body,expected_http",
    [
        ({"ret": 0, "errcode": 401, "errmsg": "SENSITIVE_UPSTREAM_DETAIL"}, 400),
        ({"ret": 0}, 200),
    ],
)
def test_http_credential_test_does_not_fabricate_receiver_health(
    api, monkeypatch, body, expected_http
):
    from integrations.channels.models import ChannelConnection

    client, factory, _ = api
    with factory() as db:
        db.add(
            ChannelConnection(
                channel_id="health-fixture",
                owner_user_id="owner",
                channel_type="weixin",
                app_id="health-fixture",
                config={"bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat"},
                status="pending",
                enabled=True,
                transport="long_conn",
            )
        )
        db.commit()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw
        ),
    )
    result = client.post("/v1/channels/bots/health-fixture/test")
    assert result.status_code == expected_http
    bot = client.get("/v1/channels/bots").json()["data"]["bots"][0]
    assert bot["status"] == ("error" if expected_http == 400 else "pending")
    assert "SENSITIVE_UPSTREAM_DETAIL" not in result.text + str(bot)


async def test_getconfig_includes_scanning_user(monkeypatch):
    import json

    def respond(request):
        assert request.url.path.endswith("/getconfig")
        body = json.loads(request.content)
        assert body["ilink_user_id"] == "fixture-user@im.wechat"
        return httpx.Response(200, json={"ret": 0})

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    conn = SimpleNamespace(app_id="fixture", config={
        "bot_token_enc": encrypt_secret("fixture"), "ilink_user_id": "fixture-user@im.wechat",
    })
    await WeixinAdapter().validate_credentials(conn)


async def test_legacy_binding_leaves_validation_to_receiver(monkeypatch):
    def unexpected_request(**kwargs):
        pytest.fail("getconfig must not be called without its required user ID")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_request)
    conn = SimpleNamespace(app_id="fixture", config={"bot_token_enc": encrypt_secret("fixture")})
    assert await WeixinAdapter().validate_credentials(conn) == {"app_id": "fixture"}


def test_qr_binding_preserves_user_for_new_and_existing_bot(api, monkeypatch):
    from core.infra.ephemeral import LocalEphemeralState
    from integrations.channels.models import ChannelConnection

    state = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: state)
    monkeypatch.setattr("integrations.channels.service._start_long_conn", lambda *args: None)

    async def start():
        return {"qrcode": "fixture-qr", "qrcode_img_content": "png"}

    async def poll(qrcode):
        return {"status": "confirmed", "bot_token": "fixture-token",
                "ilink_user_id": "fixture-user@im.wechat"}

    monkeypatch.setattr(WeixinAdapter, "start_qr_bind", staticmethod(start))
    monkeypatch.setattr(WeixinAdapter, "poll_qr_status", staticmethod(poll))
    client, factory, _ = api
    channel_id = None
    for attempt in range(2):
        started = client.post("/v1/channels/weixin/bind/start").json()["data"]
        response = client.get(f"/v1/channels/weixin/bind/{started['bind_id']}/status")
        assert response.status_code == 200
        ident = response.json()["data"]["channel_id"]
        assert channel_id is None or ident == channel_id
        channel_id = ident
        with factory() as db:
            bot = db.get(ChannelConnection, channel_id)
            assert bot.config["ilink_user_id"] == "fixture-user@im.wechat"
            if attempt == 0:
                bot.config = {key: value for key, value in bot.config.items() if key != "ilink_user_id"}
                db.commit()
    assert "fixture-user@im.wechat" not in client.get("/v1/channels/bots").text
