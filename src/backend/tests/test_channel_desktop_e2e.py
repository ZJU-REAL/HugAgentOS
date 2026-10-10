"""Four channel paths through cloud HTTP routes and the real desktop chat runtime.

Only external platform HTTP, model generation and long-connection startup are fixtures.
Cloud and local databases are separate; provider parsing and outbound adapters are real.
"""

import asyncio
import json
from contextvars import Context

import httpx
import pytest
from core.artifacts import store
from core.channels.adapters.lark import LarkAdapter
from core.channels.registry import get_adapter
from core.db.models import ChannelConnection, UserShadow
from core.infra.ephemeral import LocalEphemeralState
from core.llm import workspace
from core.services import channel_desktop_worker as worker
from core.services.artifact_service import persist_artifacts
from core.services.channel_relay_dispatch import dispatch_to_desktop
from core.services.channel_service import ChannelService
from core.storage import LocalStorageBackend
from httpx import _client
from orchestration import chat_run_executor as executor
from tests.test_channel_desktop_http import cloud  # noqa: F401
from tests.test_channel_desktop_relay_api import api  # noqa: F401
from tests.test_channel_desktop_worker import runtime  # noqa: F401

# Restore the actual methods overridden by the older API fixture.
LARK_METHODS = {
    name: getattr(LarkAdapter, name)
    for name in (
        "validate_credentials",
        "send_text",
        "download_resource",
    )
}


def inbound_payload(channel):
    message_id = "e2e-" + channel
    return {
        "lark": {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_id": message_id,
                    "chat_id": "peer",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "你好"}),
                },
                "sender": {"sender_id": {"open_id": "peer"}},
            },
        },
        "dingtalk": {
            "msgtype": "text",
            "text": {"content": "你好"},
            "msgId": message_id,
            "conversationType": "1",
            "conversationId": "peer",
            "senderStaffId": "peer",
        },
        "wecom": {
            "MsgType": "text",
            "Content": "你好",
            "MsgId": message_id,
            "FromUserName": "peer",
        },
        "weixin": {
            "message_type": 1,
            "from_user_id": "peer",
            "context_token": message_id,
            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        },
    }[channel]


@pytest.mark.parametrize("channel", ["weixin", "lark", "dingtalk", "wecom"])
async def test_binding_ingress_local_run_reply_file_and_complete(
    api,
    cloud,
    runtime,
    monkeypatch,
    channel,
    tmp_path,
):
    client, cloud_sessions, _ = api
    identity = worker.bridge.get_identity_state()
    with runtime() as db:
        db.get(UserShadow, "local-owner").user_center_id = "center"
        db.commit()
    for name, method in LARK_METHODS.items():
        monkeypatch.setattr(LarkAdapter, name, method)
    monkeypatch.setattr(ChannelService, "_start_long_conn", lambda *args: None)
    ephemeral = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: ephemeral)
    provider_requests = []
    cloud_paths = []
    qr_states = iter(["wait", "scaned", "confirmed"])
    from fastapi import FastAPI

    proxy = FastAPI()
    proxy.mount("/api", client.app)

    async def respond(request):
        if request.url.host == "cloud.invalid":
            cloud_paths.append(request.url.path)
            # Separate process boundary: cloud must not inherit the local scoped adapter.
            return await asyncio.create_task(
                httpx.ASGITransport(app=proxy).handle_async_request(request), context=Context()
            )
        provider_requests.append((request.url.host, request.url.path, request.content))
        path = request.url.path
        if path in {
            "/open-apis/im/v1/messages",
            "/v1.0/robot/oToMessages/batchSend",
            "/cgi-bin/message/send",
            "/ilink/bot/sendmessage",
        }:
            body = json.loads(request.content)
            if channel == "lark":
                assert body["receive_id"] == "peer"
            elif channel == "dingtalk":
                assert body["userIds"] == ["peer"]
            elif channel == "wecom":
                assert body["touser"] == "peer" and str(body["agentid"]) == "100001"
            else:
                assert body["msg"]["to_user_id"] == "peer"
                assert body["msg"]["context_token"] == "e2e-weixin"
        if path in {"/open-apis/im/v1/files", "/media/upload", "/cgi-bin/media/upload"}:
            assert b"local-file" in request.content
        if path == "/ilink/bot/getuploadurl":
            import hashlib

            body = json.loads(request.content)
            assert body["rawsize"] == len(b"local-file")
            assert body["rawfilemd5"] == hashlib.md5(b"local-file").hexdigest()
        key = (request.url.host, request.method, path)
        if key == ("ilinkai.weixin.qq.com", "GET", "/ilink/bot/get_bot_qrcode"):
            data = {"qrcode": "fixture-qr", "qrcode_img_content": "fixture-png"}
        elif key == ("ilinkai.weixin.qq.com", "GET", "/ilink/bot/get_qrcode_status"):
            data = {"status": next(qr_states), "bot_token": "fixture-token"}
        elif key == ("ilinkai.weixin.qq.com", "POST", "/ilink/bot/getuploadurl"):
            data = {"upload_full_url": "https://cdn.invalid/upload"}
        elif key == ("cdn.invalid", "POST", "/upload"):
            return httpx.Response(200, headers={"x-encrypted-param": "fixture-encrypted-param"})
        else:
            responses = {
                ("ilinkai.weixin.qq.com", "POST", "/ilink/bot/sendmessage"): {"ret": 0},
                ("open.feishu.cn", "POST", "/open-apis/auth/v3/tenant_access_token/internal"): {
                    "code": 0,
                    "tenant_access_token": "fixture-token",
                    "expire": 7200,
                },
                ("open.feishu.cn", "POST", "/open-apis/im/v1/messages"): {
                    "code": 0,
                    "data": {"message_id": "fixture-reply"},
                },
                ("open.feishu.cn", "PUT", "/open-apis/im/v1/messages/fixture-reply"): {"code": 0},
                ("open.feishu.cn", "POST", "/open-apis/im/v1/files"): {
                    "code": 0,
                    "data": {"file_key": "fixture-file"},
                },
                ("api.dingtalk.com", "POST", "/v1.0/oauth2/accessToken"): {
                    "accessToken": "fixture-token",
                    "expireIn": 7200,
                },
                ("api.dingtalk.com", "POST", "/v1.0/robot/oToMessages/batchSend"): {
                    "processQueryKey": "fixture-query"
                },
                ("api.dingtalk.com", "POST", "/v1.0/robot/otoMessages/batchRecall"): {},
                ("oapi.dingtalk.com", "POST", "/media/upload"): {
                    "errcode": 0,
                    "media_id": "fixture-media",
                },
                ("qyapi.weixin.qq.com", "GET", "/cgi-bin/gettoken"): {
                    "errcode": 0,
                    "access_token": "fixture-token",
                    "expires_in": 7200,
                },
                ("qyapi.weixin.qq.com", "POST", "/cgi-bin/message/send"): {"errcode": 0},
                ("qyapi.weixin.qq.com", "POST", "/cgi-bin/media/upload"): {
                    "errcode": 0,
                    "media_id": "fixture-media",
                },
            }
            assert key in responses, f"Unexpected provider request: {key}"
            data = responses[key]
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    with runtime() as db:
        grant = await worker.prepare_binding(db, "local-owner")
    if channel == "weixin":
        start = client.post(
            "/v1/channels/weixin/bind/start",
            params={"execution_location": "local", "local_binding_id": grant["binding_id"]},
        )
        assert start.status_code == 200, start.text
        bind_id = start.json()["data"]["bind_id"]
        for expected in ["waiting", "scanned", "confirmed"]:
            status = client.get(f"/v1/channels/weixin/bind/{bind_id}/status")
            assert status.json()["data"]["status"] == expected, status.text
    else:
        response = client.post(
            "/v1/channels/bots",
            json={
                "channel_type": channel,
                "app_id": "e2e-" + channel,
                "app_secret": "fixture-secret",
                "extra": {"agent_id": "100001"},
                "transport": "webhook" if channel == "wecom" else "long_conn",
                "execution_location": "local",
                "local_binding_id": grant["binding_id"],
            },
        )
        assert response.status_code == 201, response.text
    bot = client.get("/v1/channels/bots").json()["data"]["bots"][0]
    assert bot["execution_location"] == "local" and bot["device_online"]
    adapter = get_adapter(channel)
    with cloud_sessions() as db:
        conn = db.get(ChannelConnection, bot["channel_id"])
        msg = adapter.parse_inbound(conn, inbound_payload(channel))
    assert msg is not None and msg.text == "你好"
    monkeypatch.setattr("core.services.channel_relay_dispatch.bridge_enabled", lambda: False)
    assert await dispatch_to_desktop(msg, cloud_sessions)
    claim = await worker.cloud_post(cloud, "claim", {"binding_ids": [grant["binding_id"]]})
    item = claim["delivery"]
    assert item and item["message"]["text"] == "你好"
    assert "fixture-secret" not in str(item) and "fixture-token" not in str(item)
    monkeypatch.setattr("core.services.channel_relay_dispatch.bridge_enabled", lambda: True)
    seen = {}
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "storage" / "artifacts")
    storage = LocalStorageBackend()
    monkeypatch.setattr("core.storage.get_storage", lambda: storage)
    monkeypatch.setattr("core.services.artifact_service.persist_artifacts", persist_artifacts)

    async def workflow(**kwargs):
        seen.update(kwargs)
        artifact = store.save_artifact_bytes(
            content=b"local-file", name="local-proof.txt", mime_type="text/plain", extension="txt"
        )
        assert workspace.pin(
            artifact["file_id"],
            name=artifact["name"],
            mime_type=artifact["mime_type"],
            size=artifact["size"],
        )
        yield {"type": "content", "delta": "你好，本机执行成功：" + channel}
        yield {"type": "meta", "route": "main", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    assert (
        await asyncio.wait_for(
            worker.run_delivery(runtime, cloud, identity, "local-owner", item), 15
        )
        is None
    )
    assert seen["context"]["user_id"] == "local-owner"
    assert await worker.cloud_post(
        cloud, item["delivery_id"] + "/complete", {"lease": item["lease"]}
    ) == {"ok": True}
    text = "你好，本机执行成功：" + channel
    bodies = b"\n".join(body for _, _, body in provider_requests).decode("utf-8", errors="replace")
    assert text in bodies or json.dumps(text, ensure_ascii=True)[1:-1] in bodies
    assert "local-proof.txt" in bodies
    assert any(path.endswith("/renew") for path in cloud_paths)
    assert any(path.endswith("/operation") for path in cloud_paths)
    second = await worker.cloud_post(cloud, "claim", {"binding_ids": [grant["binding_id"]]})
    assert second["delivery"] is None
