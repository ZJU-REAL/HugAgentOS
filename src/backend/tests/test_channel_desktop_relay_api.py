"""HTTP contract for desktop channel binding, device isolation and idempotent replies."""

import pytest
from core.auth.backend import UserContext, get_current_user
from core.channels.protocol import InboundMsg, SendResult
from core.db.engine import Base, get_db
from core.db.models import ChannelConnection, UserShadow
from core.infra.exceptions import BadRequestError
from core.services.channel_relay import ChannelRelayService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def api(monkeypatch):
    from api.middleware.error_handler import setup_error_handlers
    from api.routes.v1.channel_desktop import router as desktop_router
    from api.routes.v1.channels import router
    from api.routes.v1.desktop_capability import _require_capability_user

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            UserShadow(
                user_id="owner", username="owner", extra_data={"can_create_channel_bot": True}
            )
        )
        db.commit()
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(router)
    app.include_router(desktop_router)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="owner", user_center_id="center", username="owner"
    )
    app.dependency_overrides[_require_capability_user] = lambda: "owner"
    sent = []

    async def validate(self, conn):
        return {}

    async def send(self, conn, msg, text):
        sent.append((msg.external_conversation_id, text))
        return SendResult.ok("reply-" + str(len(sent)))

    async def download(self, conn, msg, att):
        return b"local-fixture"

    monkeypatch.setattr("core.channels.adapters.lark.LarkAdapter.validate_credentials", validate)
    monkeypatch.setattr("core.channels.adapters.lark.LarkAdapter.send_text", send)
    monkeypatch.setattr("core.channels.adapters.lark.LarkAdapter.download_resource", download)
    with TestClient(app) as client:
        yield client, factory, sent


def headers(device="device-a"):
    return {"x-desktop-device-id": device}


def create_local(api):
    client, factory, sent = api
    grant = client.post(
        "/v1/channels/desktop/register", headers=headers(), json={"device_name": "Laptop"}
    ).json()["data"]
    response = client.post(
        "/v1/channels/bots",
        json={
            "channel_type": "lark",
            "app_id": "app",
            "app_secret": "SENSITIVE_TEST_CREDENTIAL",
            "transport": "webhook",
            "execution_location": "local",
            "local_binding_id": grant["binding_id"],
        },
    )
    assert response.status_code == 201, response.text
    return grant, response.json()["data"]


def enqueue(factory, bot, message="hello"):
    with factory() as db:
        return ChannelRelayService(db).enqueue(
            db.get(ChannelConnection, bot["channel_id"]),
            InboundMsg(
                channel_id=bot["channel_id"],
                channel_type="lark",
                text=message,
                chat_type="p2p",
                external_conversation_id="peer",
                message_id="message1",
                attachments=[{"key": "file-one", "name": "one.txt", "kind": "file"}],
            ),
        )


def test_location_requires_desktop_consent(api):
    client, _, _ = api
    response = client.post(
        "/v1/channels/bots", json={"app_id": "a", "app_secret": "b", "execution_location": "local"}
    )
    assert response.status_code == 400
    grant, bot = create_local(api)
    assert bot["execution_location"] == "local"
    assert bot["device_name"] == "Laptop"
    listed = client.get("/v1/channels/bots").json()["data"]["bots"][0]
    assert listed["device_online"] is True
    assert "binding_id" not in listed and "config" not in listed


def test_device_claim_and_reply_retry_are_isolated(api):
    client, factory, sent = api
    grant, bot = create_local(api)
    delivery = enqueue(factory, bot)
    wrong = client.post(
        "/v1/channels/desktop/claim",
        headers=headers("other-device"),
        json={"binding_ids": [grant["binding_id"]]},
    ).json()["data"]
    assert wrong["delivery"] is None
    item = client.post(
        "/v1/channels/desktop/claim", headers=headers(), json={"binding_ids": [grant["binding_id"]]}
    ).json()["data"]["delivery"]
    assert item["delivery_id"] == delivery
    assert "SENSITIVE_TEST_CREDENTIAL" not in str(item)
    payload = {
        "lease": item["lease"],
        "operation_id": "1",
        "method": "send_text",
        "args": {"text": "result"},
    }
    url = f"/v1/channels/desktop/{delivery}/operation"
    first = client.post(url, headers=headers(), json=payload)
    assert first.status_code == 200, first.text
    assert client.post(url, headers=headers(), json=payload).json()["data"] == first.json()["data"]
    assert sent == [("peer", "result")]
    assert client.post(url, headers=headers("other-device"), json=payload).status_code == 403


def test_attachment_cannot_escape_task_and_disable_revokes_lease(api):
    client, factory, _ = api
    grant, bot = create_local(api)
    delivery = enqueue(factory, bot)
    item = client.post(
        "/v1/channels/desktop/claim", headers=headers(), json={"binding_ids": [grant["binding_id"]]}
    ).json()["data"]["delivery"]
    url = f"/v1/channels/desktop/{delivery}/operation"
    payload = {
        "lease": item["lease"],
        "operation_id": "1",
        "method": "download_resource",
        "args": {"attachment": {"key": "file-one"}},
    }
    result = client.post(url, headers=headers(), json=payload)
    assert result.status_code == 200, result.text
    import base64

    assert base64.b64decode(result.json()["data"]) == b"local-fixture"
    payload["args"]["attachment"]["key"] = "private-file"
    assert client.post(url, headers=headers(), json=payload).status_code == 400
    assert (
        client.patch(f"/v1/channels/bots/{bot['channel_id']}", json={"enabled": False}).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/channels/desktop/{delivery}/renew",
            headers=headers(),
            json={"lease": item["lease"]},
        ).status_code
        == 403
    )


def test_execution_failure_is_reported_to_channel_once(api):
    client, factory, sent = api
    grant, bot = create_local(api)
    delivery = enqueue(factory, bot)
    item = client.post(
        "/v1/channels/desktop/claim", headers=headers(), json={"binding_ids": [grant["binding_id"]]}
    ).json()["data"]["delivery"]
    url = f"/v1/channels/desktop/{delivery}/complete"
    payload = {"lease": item["lease"], "error": "local agent unavailable"}
    assert client.post(url, headers=headers(), json=payload).status_code == 200
    assert len(sent) == 1 and "未完成" in sent[0][1]
    assert client.post(url, headers=headers(), json=payload).status_code == 403
    assert len(sent) == 1
