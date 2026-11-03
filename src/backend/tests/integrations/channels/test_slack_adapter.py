"""Slack adapter: event parsing, outbound Web API calls, mrkdwn conversion and Socket Mode."""

import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from httpx import _client

from core.infra.crypto import encrypt_secret
from integrations.channels.adapters import slack_socket
from integrations.channels.adapters.slack import SlackAdapter
from integrations.channels.adapters.slack_messages import markdown_to_mrkdwn
from integrations.channels.protocol import ChannelResourceError, InboundMsg
from integrations.channels.registry import get_adapter
from tests.integrations.channels.test_channel_desktop_relay_api import api  # noqa: F401


def _conn(transport="long_conn", **extra):
    config = {"app_secret_enc": encrypt_secret("xoxb-bot"), "app_token_enc": encrypt_secret("xapp-app")}
    for key, value in extra.items():
        config[f"{key}_enc"] = encrypt_secret(value)
    return SimpleNamespace(channel_id="chan_slack", app_id="A111", transport=transport, config=config)


def _event(**fields):
    return {"type": "event_callback", "event": {"type": "message", "user": "U1", "ts": "1700000000.000100", **fields}}


def _mock_api(monkeypatch, handler):
    calls = []

    def record(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(record), **kw),
    )
    return calls


def _body(request):
    if request.headers.get("content-type", "").startswith("application/json"):
        return json.loads(request.content or b"{}")
    return dict(httpx.QueryParams(request.content.decode()))


def test_registered_with_socket_mode_credentials():
    adapter = get_adapter("slack")
    assert isinstance(adapter, SlackAdapter)
    assert adapter.caps.supports_long_conn and adapter.caps.supports_group_observe
    assert adapter.caps.credential_fields == ("app_id", "app_secret", "app_token")


def test_dm_is_p2p_and_addressed():
    msg = SlackAdapter().parse_inbound(_conn(), _event(channel="D9", channel_type="im", text="hi &amp; <https://x.io|docs>"))
    assert (msg.chat_type, msg.external_conversation_id, msg.addressed_to_bot) == ("p2p", "U1", True)
    assert msg.text == "hi & docs (https://x.io)"
    assert msg.message_id == "D9:1700000000.000100"
    assert msg.raw["slack_channel"] == "D9"


def test_channel_message_defers_addressing_and_strips_leading_mention():
    msg = SlackAdapter().parse_inbound(_conn(), _event(channel="C5", channel_type="channel", text="<@UBOT> summarize <@U2>", thread_ts="1.0"))
    assert (msg.chat_type, msg.external_conversation_id) == ("group", "C5")
    assert msg.addressed_to_bot is None
    assert msg.mentioned_ids == ["UBOT", "U2"]
    assert msg.text == "summarize @U2"
    assert msg.raw["slack_thread_ts"] == "1.0"


def test_app_mention_shares_message_id_with_message_event():
    adapter = SlackAdapter()
    mention = adapter.parse_inbound(_conn(), {"type": "event_callback", "event": {"type": "app_mention", "user": "U1", "channel": "C5", "ts": "2.0", "text": "<@UBOT> hi"}})
    message = adapter.parse_inbound(_conn(), _event(channel="C5", channel_type="channel", ts="2.0", text="<@UBOT> hi"))
    assert mention.addressed_to_bot is True
    assert mention.message_id == message.message_id == "C5:2.0"


@pytest.mark.parametrize("fields", [
    {"bot_id": "B1", "text": "echo"},
    {"subtype": "message_changed", "text": "edit"},
    {"subtype": "channel_join", "text": "joined"},
    {"text": "   "},
])
def test_non_content_events_are_ignored(fields):
    assert SlackAdapter().parse_inbound(_conn(), _event(channel="C5", **fields)) is None


def test_files_become_attachments():
    msg = SlackAdapter().parse_inbound(_conn(), _event(channel="D9", channel_type="im", subtype="file_share", text="", files=[
        {"id": "F1", "name": "a.png", "mimetype": "image/png", "url_private_download": "https://files.slack.com/a.png"},
        {"id": "F2", "name": "b.pdf", "mimetype": "application/pdf", "url_private_download": "https://files.slack.com/b.pdf"},
    ]))
    assert [(a["kind"], a["name"]) for a in msg.attachments] == [("image", "a.png"), ("file", "b.pdf")]


async def test_resolve_addressed_matches_bot_user(monkeypatch):
    SlackAdapter._bot_user_cache.clear()
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "user_id": "UBOT"}))
    adapter = SlackAdapter()
    msg = adapter.parse_inbound(_conn(), _event(channel="C5", text="hello <@UBOT>"))
    other = adapter.parse_inbound(_conn(), _event(channel="C5", text="hello <@U2>"))
    assert await adapter.resolve_addressed(_conn(), msg) is True
    assert await adapter.resolve_addressed(_conn(), other) is False


async def test_validate_rejects_mismatched_app_id(monkeypatch):
    def handler(request):
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT", "bot_id": "B1"})
        return httpx.Response(200, json={"ok": True, "bot": {"app_id": "AOTHER"}})

    _mock_api(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="AOTHER"):
        await SlackAdapter().validate_credentials(_conn())


async def test_validate_requires_app_token_for_socket_mode():
    conn = _conn()
    conn.config.pop("app_token_enc")
    with pytest.raises(RuntimeError, match="xapp-"):
        await SlackAdapter().validate_credentials(conn)


async def test_validate_reports_auth_error(monkeypatch):
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))
    with pytest.raises(RuntimeError, match="invalid_auth"):
        await SlackAdapter().validate_credentials(_conn())


async def test_send_replies_in_thread_and_edit_uses_channel_ts(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True, "channel": "C5", "ts": "9.9"})

    calls = _mock_api(monkeypatch, handler)
    adapter = SlackAdapter()
    inbound = adapter.parse_inbound(_conn(), _event(channel="C5", text="<@UBOT> q", thread_ts="1.0"))
    sent = await adapter.send_text(_conn(), inbound, "thinking")
    assert sent.message_id == "C5:9.9"
    edited = await adapter.edit_message(_conn(), sent.message_id, "*done*")
    assert edited.success
    assert calls[0].headers["authorization"] == "Bearer xoxb-bot"
    assert _body(calls[0]) == {"channel": "C5", "text": "thinking", "mrkdwn": True, "thread_ts": "1.0"}
    assert calls[1].url.path.endswith("chat.update")
    assert _body(calls[1]) == {"channel": "C5", "ts": "9.9", "text": "*done*"}


@pytest.mark.parametrize("status,error,kind", [
    (200, "not_in_channel", "forbidden"),
    (429, "ratelimited", "rate_limited"),
    (200, "msg_too_long", "too_long"),
    (503, "service_unavailable", "transient"),
])
async def test_send_error_classification(monkeypatch, status, error, kind):
    _mock_api(monkeypatch, lambda r: httpx.Response(status, json={"ok": False, "error": error}))
    inbound = InboundMsg(channel_id="c", channel_type="slack", text="", chat_type="p2p", external_conversation_id="U1", raw={"slack_channel": "D9"})
    result = await SlackAdapter().send_text(_conn(), inbound, "x")
    assert (result.success, result.error_kind) == (False, kind)


async def test_push_converts_markdown_and_chunks(monkeypatch):
    counter = iter(range(100))
    calls = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "channel": "D9", "ts": f"{next(counter)}.0"}))
    inbound = InboundMsg(channel_id="c", channel_type="slack", text="", chat_type="p2p", external_conversation_id="U1", raw={"slack_channel": "D9"})
    result = await SlackAdapter().push(_conn(), inbound, "**bold**\n\n" + "word " * 1000)
    assert result.success and result.continuation_ids
    assert _body(calls[0])["text"].startswith("*bold*")
    assert all(len(_body(c)["text"]) <= SlackAdapter.caps.max_message_len for c in calls)


async def test_fetch_history_filters_and_sorts(monkeypatch):
    calls = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "messages": [
        {"type": "message", "user": "U2", "ts": "20.5", "text": "later"},
        {"type": "message", "bot_id": "B1", "ts": "15.0", "text": "bot"},
        {"type": "message", "subtype": "channel_join", "user": "U3", "ts": "12.0", "text": "joined"},
        {"type": "message", "user": "U1", "ts": "10.0", "text": "earlier"},
    ]}))
    items = await SlackAdapter().fetch_history(_conn(), "C5", since_ms=5000, limit=10)
    assert [(i.text, i.ts_ms, i.message_id) for i in items] == [("earlier", 10000, "C5:10.0"), ("later", 20500, "C5:20.5")]
    assert calls[0].url.params["oldest"] == "5.000000"


async def test_fetch_history_failure_is_empty(monkeypatch):
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"ok": False, "error": "missing_scope"}))
    assert await SlackAdapter().fetch_history(_conn(), "C5", since_ms=0, limit=10) == []


async def test_download_only_sends_token_to_slack_hosts(monkeypatch):
    calls = _mock_api(monkeypatch, lambda r: httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"}))
    adapter, inbound = SlackAdapter(), None
    assert await adapter.download_resource(_conn(), inbound, {"key": "https://evil.example/a.png"}) is None
    assert calls == []
    assert await adapter.download_resource(_conn(), inbound, {"key": "https://files.slack.com/a.png"}) == b"PNG"
    assert calls[0].headers["authorization"] == "Bearer xoxb-bot"


async def test_download_without_files_scope_explains(monkeypatch):
    _mock_api(monkeypatch, lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    with pytest.raises(ChannelResourceError, match="files:read"):
        await SlackAdapter().download_resource(_conn(), None, {"key": "https://files.slack.com/a.png"})


async def test_push_file_uses_external_upload_flow(monkeypatch):
    def handler(request):
        path = request.url.path
        if path.endswith("files.getUploadURLExternal"):
            return httpx.Response(200, json={"ok": True, "upload_url": "https://files.slack.com/upload/1", "file_id": "F9"})
        if path.endswith("files.completeUploadExternal"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text="OK")

    calls = _mock_api(monkeypatch, handler)
    inbound = InboundMsg(channel_id="c", channel_type="slack", text="", chat_type="group", external_conversation_id="C5", raw={"slack_channel": "C5", "slack_thread_ts": "1.0"})
    result = await SlackAdapter().push_file(_conn(), inbound, b"data", "报告.pdf", "application/pdf")
    assert result.success and result.message_id == "F9"
    assert _body(calls[0]) == {"filename": "报告.pdf", "length": "4"}
    assert calls[1].content == b"data"
    complete = _body(calls[2])
    assert json.loads(complete["files"]) == [{"id": "F9", "title": "报告.pdf"}]
    assert (complete["channel_id"], complete["thread_ts"]) == ("C5", "1.0")


def test_webhook_signature(monkeypatch):
    conn = _conn(transport="webhook", signing_secret="shh")
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"shh", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    adapter = SlackAdapter()
    assert adapter.verify_webhook(conn, {"x-slack-request-timestamp": ts, "x-slack-signature": sig}, body)
    assert not adapter.verify_webhook(conn, {"x-slack-request-timestamp": ts, "x-slack-signature": "v0=bad"}, body)
    stale = str(int(time.time()) - 3600)
    old_sig = "v0=" + hmac.new(b"shh", b"v0:" + stale.encode() + b":" + body, hashlib.sha256).hexdigest()
    assert not adapter.verify_webhook(conn, {"x-slack-request-timestamp": stale, "x-slack-signature": old_sig}, body)


@pytest.mark.parametrize("source,expected", [
    ("**bold** and *it* and ~~gone~~", "*bold* and _it_ and ~gone~"),
    ("## Title\n- a\n- b", "*Title*\n• a\n• b"),
    ("see [docs](https://x.io) & <tag>", "see <https://x.io|docs> &amp; &lt;tag&gt;"),
    ("`**raw**` stays", "`**raw**` stays"),
    ("```python\nx = a < b\n```", "```\nx = a &lt; b\n```"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"),
])
def test_markdown_to_mrkdwn(source, expected):
    assert markdown_to_mrkdwn(source) == expected


def test_socket_mode_acks_envelopes_and_dispatches(monkeypatch):
    """Drive the real client against a local websocket server speaking Socket Mode."""
    from websockets.sync.server import serve

    acks, received = [], []
    connections = []

    def handler(ws):
        connections.append(ws)
        ws.send(json.dumps({"type": "hello"}))
        if len(connections) == 1:
            ws.send(json.dumps({"type": "events_api", "envelope_id": "E1", "payload": _event(channel="D9", channel_type="im", text="ping")}))
            acks.append(json.loads(ws.recv(timeout=5)))
            ws.send(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
            return
        ws.send(json.dumps({"type": "events_api", "envelope_id": "E2", "payload": _event(channel="D9", channel_type="im", ts="2.0", text="after refresh")}))
        acks.append(json.loads(ws.recv(timeout=5)))
        try:
            ws.recv(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr(slack_socket, "open_socket_url", lambda token: f"ws://127.0.0.1:{port}")
        client = SlackAdapter().make_ws_client(_conn(), received.append)
        connected = []
        client.set_status_handler(lambda: connected.append(True))
        worker = threading.Thread(target=client.start, daemon=True)
        worker.start()
        deadline = time.time() + 10
        while len(received) < 2 and time.time() < deadline:
            time.sleep(0.05)
        client.stop()
        worker.join(timeout=10)
        server.shutdown()

    assert [m.text for m in received] == ["ping", "after refresh"]
    assert acks == [{"envelope_id": "E1"}, {"envelope_id": "E2"}]
    assert len(connections) == 2 and len(connected) == 2
    assert not worker.is_alive()


def test_socket_url_error_is_raised(monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
    )
    with pytest.raises(RuntimeError, match="invalid_auth"):
        slack_socket.open_socket_url("xapp-bad")
