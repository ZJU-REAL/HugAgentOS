"""Channel lifecycle across HTTP workers and the elected receiver."""

import asyncio
import json
import threading
import time

import httpx
from integrations.channels.manager import ChannelManager
from core.db.engine import Base
from core.db.models import UserShadow
from core.infra.ephemeral import LocalEphemeralState
from httpx import _client
from sqlalchemy import create_engine
from tests.integrations.channels.test_channel_desktop_relay_api import api, headers  # noqa: F401


async def eventually(check, seconds=2):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.01)


async def test_binding_on_non_owner_is_received_and_claimed_on_desktop(api, monkeypatch, tmp_path):
    client, factory, _ = api
    # Separate connections are essential: workers must not share one SQLite transaction.
    engine = create_engine(f"sqlite:///{tmp_path / 'channels.sqlite'}")
    Base.metadata.create_all(engine)
    factory.configure(bind=engine)
    with factory() as db:
        db.add(
            UserShadow(
                user_id="owner", username="owner", extra_data={"can_create_channel_bot": True}
            )
        )
        db.commit()
    state = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: state)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr('integrations.channels.inbound.SessionLocal', factory)
    monkeypatch.setattr('integrations.channels.manager._RECONCILE_SECONDS', 0.02, raising=False)
    follower = ChannelManager()
    monkeypatch.setattr('integrations.channels.manager._manager', follower)
    ready = threading.Event()
    entered_poll = threading.Event()
    delivered = threading.Event()
    polls = [0]
    failures = [0]
    receive_times = []

    def respond(request):
        path = request.url.path
        if path.endswith("/get_bot_qrcode"):
            data = {"qrcode": "qr", "qrcode_img_content": "png"}
        elif path.endswith("/get_qrcode_status"):
            data = {"status": "confirmed", "bot_token": "fixture-token"}
        elif path.endswith("/getconfig"):
            data = {"ret": 0}
        else:
            assert path.endswith("/getupdates")
            polls[0] += 1
            entered_poll.set()
            if not ready.wait(2):
                raise httpx.ReadTimeout("fixture")
            receive_times.append(time.monotonic())
            if failures[0]:
                failures[0] -= 1
                return httpx.Response(200, json={"ret": -14, "err_msg": "PRIVATE_DETAIL"})
            data = {"ret": 0, "msgs": []}
            if not delivered.is_set():
                delivered.set()
                data["msgs"] = [
                    {
                        "message_type": 1,
                        "from_user_id": "fixture-peer",
                        "context_token": "fixture-message",
                        "item_list": [{"type": 1, "text_item": {"text": "hello from wechat"}}],
                    }
                ]
            else:
                threading.Event().wait(0.02)
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _client.Client(transport=httpx.MockTransport(respond), **kw)
    )
    leader = ChannelManager()
    await leader.start_all()
    try:
        grant = client.post(
            "/v1/channels/desktop/register", headers=headers(), json={"device_name": "Fixture Mac"}
        ).json()["data"]
        started = client.post(
            "/v1/channels/weixin/bind/start",
            params={"execution_location": "local", "local_binding_id": grant["binding_id"]},
        ).json()["data"]
        bound = client.get(f"/v1/channels/weixin/bind/{started['bind_id']}/status")
        assert bound.status_code == 200
        bot = client.get("/v1/channels/bots").json()["data"]["bots"][0]
        assert bot["status"] == "pending"
        await eventually(entered_poll.is_set)
        tested = client.post(f"/v1/channels/bots/{bot['channel_id']}/test")
        assert tested.status_code == 200
        assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "pending"
        ready.set()
        item = None

        def claimed():
            nonlocal item
            item = client.post(
                "/v1/channels/desktop/claim",
                headers=headers(),
                json={"binding_ids": [grant["binding_id"]]},
            ).json()["data"]["delivery"]
            return item is not None

        await eventually(claimed)
        assert item["message"]["text"] == "hello from wechat"
        assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "connected"
        assert "fixture-token" not in json.dumps(item)
        url = f"/v1/channels/bots/{bot['channel_id']}"
        failures[0] = 2
        await eventually(
            lambda: client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "error"
        )
        assert client.post(url + "/test").status_code == 200
        assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "error"
        failure_start = len(receive_times) - 1
        await eventually(
            lambda: client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"]
            == "connected",
            seconds=5,
        )
        retries = receive_times[failure_start : failure_start + 3]
        assert len(retries) == 3
        assert retries[1] - retries[0] >= 0.8
        assert retries[2] - retries[1] >= 1.8
        assert "PRIVATE_DETAIL" not in str(client.get("/v1/channels/bots").json())
        # Both API mutations happen before the leader's reconciliation can run.
        client.patch(url, json={"enabled": False})
        client.patch(url, json={"enabled": True})
        await eventually(
            lambda: client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"]
            == "connected"
        )
        client.patch(url, json={"enabled": False})
        await asyncio.sleep(0.15)
        count = polls[0]
        await asyncio.sleep(0.1)
        assert polls[0] == count
        assert client.post(url + "/test").status_code == 200
        assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "disconnected"
        client.patch(url, json={"enabled": True})
        await eventually(lambda: polls[0] > count)
        await eventually(
            lambda: client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"]
            == "connected"
        )
        client.patch(url, json={"enabled": True})
        assert client.get("/v1/channels/bots").json()["data"]["bots"][0]["status"] == "connected"
        leader.shutdown()
        await asyncio.sleep(0.15)
        count = polls[0]
        await asyncio.sleep(0.1)
        assert polls[0] == count
        await leader.start_all()
        await eventually(lambda: polls[0] > count)
        client.delete(url)
        await asyncio.sleep(0.15)
        count = polls[0]
        await asyncio.sleep(0.1)
        assert polls[0] == count
        assert client.get("/v1/channels/bots").json()["data"]["bots"] == []

    finally:
        leader.shutdown()
        ready.set()
        await asyncio.sleep(0.08)
        engine.dispose()
