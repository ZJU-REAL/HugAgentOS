"""WeChat HTTP business errors must not pass credential validation."""

from types import SimpleNamespace

import httpx
import pytest
from core.channels.adapters.weixin import WeixinAdapter
from core.infra.crypto import encrypt_secret
from httpx import _client
from tests.test_channel_desktop_relay_api import api  # noqa: F401


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
    conn = SimpleNamespace(app_id="fixture", config={"bot_token_enc": encrypt_secret("fixture")})
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
        channel_id="fixture", config={"bot_token_enc": encrypt_secret("fixture")}
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
    conn = SimpleNamespace(app_id="fixture", config={"bot_token_enc": encrypt_secret("fixture")})
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
        channel_id="fixture", config={"bot_token_enc": encrypt_secret("fixture")}
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
    from core.db.models import ChannelConnection

    client, factory, _ = api
    with factory() as db:
        db.add(
            ChannelConnection(
                channel_id="health-fixture",
                owner_user_id="owner",
                channel_type="weixin",
                app_id="health-fixture",
                config={"bot_token_enc": encrypt_secret("fixture")},
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
