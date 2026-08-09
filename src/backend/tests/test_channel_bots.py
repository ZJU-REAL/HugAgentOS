"""Unit tests for inbound channel bots (owner service-account model).

Covers: capability flags, repository CRUD, service capability gating + token
lock, resource-whitelist normalization, and the Lark adapter's event
normalization / @-mention stripping / AES decryption / session keying.

No network calls: create_bot's credential validation needs HTTP, so only
the pre-network branches are tested (capability gating / token lock).
"""

import asyncio
import base64
import hashlib
import json
import re

import pytest

from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS, resolve_capabilities
from core.channels.adapters.lark import LarkAdapter
from core.channels.protocol import InboundMsg, SendResult
from core.db.models import ChannelConnection, UserShadow
from core.db.repository.channel import ChannelConnectionRepository
from core.infra.exceptions import AccessDeniedError, BadRequestError
from core.services.channel_service import ChannelService, _clean_scope, bot_to_dict


# ── Capability flags ─────────────────────────────────────────────────────
def test_capability_bit_registered_default_false():
    assert "can_create_channel_bot" in BOOL_CAPABILITY_DEFAULTS
    assert BOOL_CAPABILITY_DEFAULTS["can_create_channel_bot"] is False


def test_capability_personal_override_wins():
    caps = resolve_capabilities({"can_create_channel_bot": True}, {})
    assert caps["can_create_channel_bot"] is True
    caps2 = resolve_capabilities({}, {"can_create_channel_bot": True})  # team default
    assert caps2["can_create_channel_bot"] is True


# ── repository ──────────────────────────────────────────────────────────
def _mk_user(db, uid="u1", meta=None):
    db.add(UserShadow(user_id=uid, username=uid, extra_data=meta or {}))
    db.commit()


def test_repo_crud(db_session):
    _mk_user(db_session)
    repo = ChannelConnectionRepository(db_session)
    conn = repo.create({
        "channel_id": "chan_a", "owner_user_id": "u1", "channel_type": "lark",
        "app_id": "cli_x", "config": {"app_secret_enc": "x"}, "status": "pending",
    })
    assert repo.get_by_id("chan_a") is conn
    assert repo.get_by_app_id("lark", "cli_x").channel_id == "chan_a"
    assert [c.channel_id for c in repo.list_by_owner("u1")] == ["chan_a"]

    repo.set_status("chan_a", "connected")
    assert repo.get_by_id("chan_a").status == "connected"
    repo.set_status("chan_a", "error", last_error="boom")
    assert repo.get_by_id("chan_a").last_error == "boom"

    assert repo.delete("chan_a") is True
    assert repo.get_by_id("chan_a") is None


# ── Resource-whitelist normalization ─────────────────────────────────────
def test_clean_scope():
    assert _clean_scope(None) is None
    assert _clean_scope({}) is None
    assert _clean_scope({"kb_ids": []}) is None
    assert _clean_scope({"kb_ids": ["k1"], "junk": 1}) == {"kb_ids": ["k1"]}
    assert _clean_scope({"skill_ids": [1, 2]}) == {"skill_ids": ["1", "2"]}


# ── service: capability gating + token lock (pre-network branches) ───────
def test_create_bot_denied_without_capability(db_session):
    _mk_user(db_session, "u_deny", meta={})
    svc = ChannelService(db_session)
    with pytest.raises(AccessDeniedError):
        asyncio.run(svc.create_bot(
            "u_deny", channel_type="lark", app_id="cli_a", app_secret="s",
        ))


def test_create_bot_token_lock(db_session):
    _mk_user(db_session, "u_ok", meta={"can_create_channel_bot": True})
    # Pre-occupy the same app
    ChannelConnectionRepository(db_session).create({
        "channel_id": "chan_pre", "owner_user_id": "u_ok", "channel_type": "lark",
        "app_id": "cli_dup", "config": {}, "status": "connected",
    })
    svc = ChannelService(db_session)
    with pytest.raises(BadRequestError):
        asyncio.run(svc.create_bot(
            "u_ok", channel_type="lark", app_id="cli_dup", app_secret="s",
        ))


def test_create_bot_rejects_unknown_channel(db_session):
    _mk_user(db_session, "u_ch", meta={"can_create_channel_bot": True})
    svc = ChannelService(db_session)
    with pytest.raises(BadRequestError):
        asyncio.run(svc.create_bot(
            "u_ch", channel_type="telegram", app_id="a", app_secret="s",
        ))


def test_bot_to_dict_hides_secrets(db_session):
    _mk_user(db_session, "u_d")
    conn = ChannelConnectionRepository(db_session).create({
        "channel_id": "chan_d", "owner_user_id": "u_d", "channel_type": "lark",
        "app_id": "cli_d", "config": {"app_secret_enc": "SECRET"}, "status": "connected",
    })
    d = bot_to_dict(conn)
    assert "config" not in d and "app_secret_enc" not in json.dumps(d)
    assert d["app_id"] == "cli_d" and d["status"] == "connected"
    assert d["agent_id"] is None  # defaults to the main agent


# ── Sub-agent binding (agent_id) ──────────────────────────────────────────
def test_repo_list_filters_by_agent(db_session):
    _mk_user(db_session, "u_f")
    repo = ChannelConnectionRepository(db_session)
    repo.create({
        "channel_id": "chan_main", "owner_user_id": "u_f", "channel_type": "lark",
        "app_id": "cli_main", "config": {}, "status": "connected", "agent_id": None,
    })
    repo.create({
        "channel_id": "chan_ag", "owner_user_id": "u_f", "channel_type": "lark",
        "app_id": "cli_ag", "config": {}, "status": "connected", "agent_id": "ag_1",
    })
    assert {c.channel_id for c in repo.list_by_owner("u_f")} == {"chan_main", "chan_ag"}
    assert [c.channel_id for c in repo.list_by_owner("u_f", main_only=True)] == ["chan_main"]
    assert [c.channel_id for c in repo.list_by_owner("u_f", agent_id="ag_1")] == ["chan_ag"]


def test_create_bot_rejects_unknown_agent(db_session):
    _mk_user(db_session, "u_ag", meta={"can_create_channel_bot": True})
    svc = ChannelService(db_session)
    # agent_id pointing to a nonexistent/inaccessible sub-agent → rejected before any network call
    with pytest.raises(BadRequestError):
        asyncio.run(svc.create_bot(
            "u_ag", channel_type="lark", app_id="cli_ag2", app_secret="s",
            agent_id="nope_agent",
        ))


def test_inbound_resource_scope_ignored_marker_for_agent(db_session):
    """A conn bound to a sub-agent: agent_id lands on the row, for inbound to set context['agent_id'] pinning that sub-agent.

    (Inbound's context assembly goes through the global SessionLocal; the
    unit test only asserts at the row level that the binding is persisted;
    the pinning branch `if conn.agent_id: context['agent_id']=conn.agent_id`
    is a direct attribute read.)
    """
    conn = _mk_conn(db_session)
    assert conn.agent_id is None  # _mk_conn defaults to the main agent
    conn.agent_id = "ag_pinned"
    db_session.commit()
    reloaded = ChannelConnectionRepository(db_session).get_by_id("chan_in")
    assert reloaded.agent_id == "ag_pinned"


# ── Lark adapter ──────────────────────────────────────────────────────────
class _FakeConn:
    channel_id = "chan_lark"
    channel_type = "lark"
    app_id = "cli_z"
    config = {}


def test_lark_extract_text_strips_mention():
    assert LarkAdapter._extract_text(json.dumps({"text": "@_user_1 你好"})) == "你好"
    assert LarkAdapter._extract_text(json.dumps({"text": "纯文本"})) == "纯文本"
    assert LarkAdapter._extract_text(None) == ""


def test_lark_parse_inbound_group():
    adapter = LarkAdapter()
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_group1", "chat_type": "group",
                "message_id": "om_1", "message_type": "text",
                "content": json.dumps({"text": "@_user_1 汇报一下"}),
            },
            "sender": {"sender_id": {"open_id": "ou_alice"}},
        },
    }
    msg = adapter.parse_inbound(_FakeConn(), payload)
    assert isinstance(msg, InboundMsg)
    assert msg.text == "汇报一下"
    assert msg.chat_type == "group"
    assert msg.external_conversation_id == "oc_group1"   # groups are keyed by chat_id
    assert msg.sender_id == "ou_alice"
    assert msg.raw["lark_chat_id"] == "oc_group1"


def test_lark_parse_inbound_skips_non_message():
    adapter = LarkAdapter()
    assert adapter.parse_inbound(_FakeConn(), {"type": "url_verification"}) is None
    # Non-text messages are skipped
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"message_type": "image", "chat_id": "x"}, "sender": {}},
    }
    assert adapter.parse_inbound(_FakeConn(), payload) is None


def test_lark_aes_decrypt_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encrypt_key = "my-encrypt-key"
    plain = json.dumps({"type": "url_verification", "challenge": "abc"}).encode("utf-8")
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = b"\x00" * 16
    pad = 16 - (len(plain) % 16)
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    token = base64.b64encode(iv + ct).decode()

    out = LarkAdapter._aes_decrypt(token, encrypt_key)
    assert out["challenge"] == "abc"


def test_chunk_text():
    from core.channels.protocol import chunk_text
    assert chunk_text("", 100) == []
    assert chunk_text("hello", 100) == ["hello"]
    # Over-length → multiple chunks, each within the limit
    parts = chunk_text("x" * 950, 200)
    assert len(parts) == 5 and all(len(p) <= 200 for p in parts)
    # Prefer breaking at a newline
    parts2 = chunk_text("a" * 150 + "\n" + "b" * 50, 200)
    assert parts2[0] == "a" * 150  # cut at the newline, without the b's


def test_speaker_label():
    from core.channels.inbound import _speaker_label
    from core.channels.protocol import InboundMsg
    m = InboundMsg(channel_id="c", channel_type="lark", text="hi", chat_type="group",
                   external_conversation_id="oc", sender_id="ou_xyz789", sender_name="张三")
    assert _speaker_label(m) == "张三"
    m2 = InboundMsg(channel_id="c", channel_type="lark", text="hi", chat_type="group",
                    external_conversation_id="oc", sender_id="ou_xyz789")
    assert _speaker_label(m2) == "xyz789"  # no name → tail segment of the open_id


def test_conv_lock_serializes_same_conversation():
    """#1: same-conversation locks serialize; different conversations run concurrently."""
    import asyncio
    from core.channels import inbound as inb

    order = []

    async def _fake(msg, tag, delay):
        async with inb._conv_locks[inb._conv_key(msg)]:
            order.append(f"{tag}-start")
            await asyncio.sleep(delay)
            order.append(f"{tag}-end")

    def _msg(conv):
        from core.channels.protocol import InboundMsg
        return InboundMsg(channel_id="c", channel_type="lark", text="x", chat_type="group",
                          external_conversation_id=conv)

    async def _run():
        same1, same2 = _msg("A"), _msg("A")
        await asyncio.gather(_fake(same1, "a1", 0.05), _fake(same2, "a2", 0.01))

    asyncio.run(_run())
    # Same conversation: a1 must fully finish before a2 starts (serial)
    assert order == ["a1-start", "a1-end", "a2-start", "a2-end"]


def test_outbound_synthetic_msg_targets_conversation():
    """#7: the proactively delivered placeholder message lets the adapter locate the target chat_id."""
    from core.channels.outbound import _synthetic_msg
    from core.channels.adapters.lark import LarkAdapter
    m = _synthetic_msg("chan_x", "lark", "oc_group9")
    assert m.external_conversation_id == "oc_group9"
    # The adapter uses raw.lark_chat_id to locate the send target
    assert LarkAdapter()._chat_id(m) == "oc_group9"


def test_send_result_helpers():
    ok = SendResult.ok("m1")
    assert ok.success and ok.message_id == "m1"
    bad = SendResult.fail("nonsense")
    assert not bad.success and bad.error_kind == "unknown"
    rl = SendResult.fail("rate_limited", "too fast")
    assert rl.error_kind == "rate_limited"


# ── inbound session keying (multi-tenant core: p2p one per person / group one per group) ──
def _mk_conn(db, owner="u_o", scope=None):
    _mk_user(db, owner, meta={"can_create_channel_bot": True})
    return ChannelConnectionRepository(db).create({
        "channel_id": "chan_in", "owner_user_id": owner, "channel_type": "lark",
        "app_id": "cli_in", "config": {}, "status": "connected", "resource_scope": scope,
    })


def _inbound(conv_id, chat_type, text="hi", mid="m1"):
    return InboundMsg(
        channel_id="chan_in", channel_type="lark", text=text, chat_type=chat_type,
        external_conversation_id=conv_id, sender_id="ou_x", message_id=mid,
    )


def test_inbound_session_keying(db_session):
    from core.channels.inbound import _find_or_create_session
    conn = _mk_conn(db_session)

    # Two messages in the same conversation → reuse the same chat_id, held by the owner
    s1 = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group"))
    s2 = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group", mid="m2"))
    assert s1.chat_id == s2.chat_id
    assert s1.user_id == "u_o"
    assert s1.channel_id == "chan_in" and s1.external_conversation_id == "oc_g1"

    # A different conversation (another person's DM / another group) → separate chat_id
    s3 = _find_or_create_session(db_session, conn, _inbound("oc_p2", "p2p"))
    assert s3.chat_id != s1.chat_id


def test_lark_parse_file_and_image_messages():
    adapter = LarkAdapter()
    base = {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "message": {"chat_id": "oc_1", "chat_type": "p2p", "message_id": "om_9"},
        "sender": {"sender_id": {"open_id": "ou_a"}}}}
    # file message
    import copy
    fmsg = copy.deepcopy(base)
    fmsg["event"]["message"]["message_type"] = "file"
    fmsg["event"]["message"]["content"] = json.dumps({"file_key": "fk_1", "file_name": "数据.xlsx"})
    m = adapter.parse_inbound(_FakeConn(), fmsg)
    assert m.text == "" and m.attachments == [{"kind": "file", "key": "fk_1", "name": "数据.xlsx"}]
    assert m.raw["lark_message_id"] == "om_9"
    # image message
    imsg = copy.deepcopy(base)
    imsg["event"]["message"]["message_type"] = "image"
    imsg["event"]["message"]["content"] = json.dumps({"image_key": "ik_2"})
    m2 = adapter.parse_inbound(_FakeConn(), imsg)
    assert m2.attachments[0]["kind"] == "image" and m2.attachments[0]["key"] == "ik_2"


def test_lark_parse_post_extracts_text_and_images():
    adapter = LarkAdapter()
    post = {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "message": {"chat_id": "oc_1", "chat_type": "group", "message_id": "om_p",
                    "message_type": "post",
                    "content": json.dumps({"title": "周报", "content": [
                        [{"tag": "text", "text": "完成了"}, {"tag": "img", "image_key": "ik_x"}],
                    ]})},
        "sender": {"sender_id": {"open_id": "ou_a"}}}}
    m = adapter.parse_inbound(_FakeConn(), post)
    assert "周报" in m.text and "完成了" in m.text
    assert any(a["key"] == "ik_x" for a in m.attachments)


def test_inbound_ingest_attachments_stores_artifact(db_session):
    import asyncio
    from core.channels import inbound as inb
    from core.db.models import Artifact

    conn = _mk_conn(db_session)

    class _Adapter:
        async def download_resource(self, conn, msg, att):
            return b"hello world bytes"

    # mock storage (avoid real disk writes) + parse_file
    class _Storage:
        def upload_bytes(self, content, key):
            return f"/local/{key}"
    import core.storage as cs
    orig_get = cs.get_storage
    cs.get_storage = lambda: _Storage()
    try:
        msg = _inbound("oc_1", "p2p", text="", mid="mf1")
        msg.attachments = [{"kind": "file", "key": "fk_1", "name": "note.txt"}]
        files = asyncio.run(inb._ingest_attachments(db_session, _Adapter(), conn, "u_o", "chat_x", msg))
    finally:
        cs.get_storage = orig_get

    assert len(files) == 1
    f = files[0]
    assert f["name"] == "note.txt" and f["file_id"].startswith("ua_")
    assert f["download_url"] == f"/files/{f['file_id']}"
    # Artifact persisted to DB
    row = db_session.query(Artifact).filter(Artifact.artifact_id == f["file_id"]).first()
    assert row is not None and row.user_id == "u_o" and row.chat_id == "chat_x"
    assert row.extra_data["source"] == "channel_upload"
    assert row.size_bytes == len(b"hello world bytes")


def test_inbound_resource_scope_narrows(db_session):
    from core.channels.inbound import _resolve_enabled
    # No whitelist → enabled_kbs=None (everything), enabled_skills resolved via the owner
    conn = _mk_conn(db_session)
    out = _resolve_enabled(db_session, conn, "u_o")
    assert out["enabled_kbs"] is None

    # With a whitelist → narrowed to the specified kb/skill
    conn.resource_scope = {"kb_ids": ["k1", "k2"], "skill_ids": ["sk1"]}
    db_session.commit()
    out2 = _resolve_enabled(db_session, conn, "u_o")
    assert out2["enabled_kbs"] == ["k1", "k2"]
    assert out2["enabled_skills"] == ["sk1"]


# ── DingTalk adapter ─────────────────────────────────────────────────────
class _FakeDTConn:
    channel_id = "chan_dt"
    channel_type = "dingtalk"
    app_id = "dingkey"
    config = {}


def test_dingtalk_parse_inbound_group_and_session_webhook():
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    payload = {
        "msgtype": "text",
        "text": {"content": " 汇报一下 "},
        "conversationType": "2",
        "conversationId": "cidGROUP",
        "senderStaffId": "staff_a",
        "senderNick": "Alice",
        "msgId": "msg_1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/send?session=xyz",
    }
    msg = adapter.parse_inbound(_FakeDTConn(), payload)
    assert isinstance(msg, InboundMsg)
    assert msg.text == "汇报一下"
    assert msg.chat_type == "group"
    assert msg.external_conversation_id == "cidGROUP"
    assert msg.sender_name == "Alice"
    assert msg.raw["dingtalk_session_webhook"].endswith("session=xyz")


def test_dingtalk_parse_inbound_p2p_and_skips_non_text():
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    p2p = adapter.parse_inbound(_FakeDTConn(), {
        "msgtype": "text", "text": {"content": "hi"}, "conversationType": "1",
        "conversationId": "cidP2P", "msgId": "m2",
    })
    assert p2p.chat_type == "p2p"
    # Images and other non-text → None (v1 is text only)
    assert adapter.parse_inbound(_FakeDTConn(), {"msgtype": "picture"}) is None


def test_dingtalk_edit_message_unsupported():
    from core.channels.adapters.dingtalk import DingTalkAdapter

    r = asyncio.run(DingTalkAdapter().edit_message(_FakeDTConn(), "mid", "x"))
    assert r.success is False and r.error_kind == "bad_format"


def test_dingtalk_has_push_file():
    # Inbound orchestration decides whether to return generated files by hasattr(adapter, "push_file") — if missing, it is silently skipped
    from core.channels.adapters.dingtalk import DingTalkAdapter

    assert callable(getattr(DingTalkAdapter(), "push_file", None))


def test_dingtalk_robot_send_requires_target():
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    group = InboundMsg(channel_id="chan_dt", channel_type="dingtalk", text="",
                       chat_type="group", external_conversation_id="")
    r = asyncio.run(adapter._robot_send("tok", _FakeDTConn(), group, "sampleFile", {}))
    assert r.success is False and r.error_kind == "bad_format"

    # p2p with neither staffId nor openConversationId → bad_format
    # (with an openConversationId it goes through privateChatMessages/send, a network call, not covered by unit tests)
    p2p = InboundMsg(channel_id="chan_dt", channel_type="dingtalk", text="",
                     chat_type="p2p", external_conversation_id="", sender_id="")
    r2 = asyncio.run(adapter._robot_send("tok", _FakeDTConn(), p2p, "sampleFile", {}))
    assert r2.success is False and r2.error_kind == "bad_format"


def test_dingtalk_caps_supports_markdown():
    from core.channels.adapters.dingtalk import DingTalkAdapter

    assert DingTalkAdapter.caps.supports_markdown is True


def test_dingtalk_send_markdown_via_session_webhook(monkeypatch):
    """Reply scenario (with sessionWebhook): msgtype=markdown + title extraction + table downgrade."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    captured = {}

    async def fake_post(webhook, payload):
        captured.update(webhook=webhook, payload=payload)
        return SendResult.ok()

    monkeypatch.setattr(adapter, "_post_webhook", fake_post)
    msg = InboundMsg(
        channel_id="chan_dt", channel_type="dingtalk", text="", chat_type="p2p",
        external_conversation_id="cid1",
        raw={"dingtalk_session_webhook": "https://oapi.dingtalk.com/robot/send?s=1"},
    )
    md = "# 日报\n\n| 指标 | 值 |\n| --- | --- |\n| 完成率 | 98% |"
    r = asyncio.run(adapter.send_markdown(_FakeDTConn(), msg, md))
    assert r.success
    assert captured["payload"]["msgtype"] == "markdown"
    body = captured["payload"]["markdown"]
    assert body["title"] == "日报"
    assert "- 指标: 完成率｜值: 98%" in body["text"]   # table downgraded
    assert "| --- |" not in body["text"]


def test_dingtalk_send_markdown_proactive_uses_sample_markdown(monkeypatch):
    """Proactive delivery (no sessionWebhook) → robot API sampleMarkdown."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    captured = {}

    async def fake_token(app_id, secret):
        return "tok"

    async def fake_robot_send(token, conn, inbound, msg_key, msg_param):
        captured.update(msg_key=msg_key, msg_param=msg_param)
        return SendResult.ok()

    monkeypatch.setattr(adapter, "_access_token", fake_token)
    monkeypatch.setattr(adapter, "_robot_send", fake_robot_send)
    msg = InboundMsg(channel_id="chan_dt", channel_type="dingtalk", text="",
                     chat_type="group", external_conversation_id="cidG")
    r = asyncio.run(adapter.send_markdown(_FakeDTConn(), msg, "**每日简报**\n- 事项一"))
    assert r.success
    assert captured["msg_key"] == "sampleMarkdown"
    assert captured["msg_param"]["title"] == "每日简报"
    assert "**每日简报**" in captured["msg_param"]["text"]


def test_dingtalk_push_sends_markdown_chunks(monkeypatch):
    """push (the automation proactive-delivery entry point) also sends via markdown."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    sent = []

    async def fake_send_md(conn, inbound, text):
        sent.append(text)
        return SendResult.ok()

    monkeypatch.setattr(adapter, "send_markdown", fake_send_md)
    msg = InboundMsg(channel_id="chan_dt", channel_type="dingtalk", text="",
                     chat_type="group", external_conversation_id="cidG")
    r = asyncio.run(adapter.push(_FakeDTConn(), msg, "## 标题\n正文"))
    assert r.success and sent == ["## 标题\n正文"]


def test_deliver_reply_prefers_markdown_and_strips_refs():
    """_deliver_reply: channels that support markdown go through send_markdown, and [ref:...] markers are cleaned."""
    from core.channels.inbound import _deliver_reply

    calls = {"md": [], "text": []}

    class _MdAdapter:
        class caps:
            supports_markdown = True
            max_message_len = 4000

        @staticmethod
        def prepare_markdown(text):
            return text

        async def send_markdown(self, conn, msg, text):
            calls["md"].append(text)
            return SendResult.ok()

        async def send_text(self, conn, msg, text):
            calls["text"].append(text)
            return SendResult.ok()

        async def edit_message(self, conn, mid, text):
            return SendResult.fail("bad_format", "不支持")

    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="",
                     chat_type="p2p", external_conversation_id="cid")
    asyncio.run(_deliver_reply(_MdAdapter(), object(), msg,
                               "结论如下[ref:internet_search-1]，见上文。", "ph_1"))
    assert calls["md"] == ["结论如下，见上文。"]   # went via markdown + citation markers cleaned
    assert calls["text"] == []


def test_dingtalk_send_placeholder_prefers_robot_api(monkeypatch):
    """Placeholder via the robot API: obtains the processQueryKey (used as message_id) → recallable afterwards."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()

    async def fake_token(app_id, secret):
        return "tok"

    async def fake_robot_send(token, conn, inbound, msg_key, msg_param):
        assert msg_key == "sampleText"
        return SendResult.ok("pqk_123")

    monkeypatch.setattr(adapter, "_access_token", fake_token)
    monkeypatch.setattr(adapter, "_robot_send", fake_robot_send)
    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="p2p",
                     external_conversation_id="cid", sender_id="staff_1")
    r = asyncio.run(adapter.send_placeholder(_FakeDTConn(), msg, "🤔 正在处理"))
    assert r.success and r.message_id == "pqk_123"


def test_dingtalk_send_placeholder_falls_back_to_webhook(monkeypatch):
    """Robot API unavailable (token failure) → fall back to sessionWebhook plain text (no id, not recallable)."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    captured = {}

    async def fake_token(app_id, secret):
        raise RuntimeError("no permission")

    async def fake_post(webhook, payload):
        captured.update(payload=payload)
        return SendResult.ok()

    monkeypatch.setattr(adapter, "_access_token", fake_token)
    monkeypatch.setattr(adapter, "_post_webhook", fake_post)
    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="p2p",
                     external_conversation_id="cid",
                     raw={"dingtalk_session_webhook": "https://oapi.dingtalk.com/robot/send?s=1"})
    r = asyncio.run(adapter.send_placeholder(_FakeDTConn(), msg, "🤔 正在处理"))
    assert r.success and r.message_id is None
    assert captured["payload"]["msgtype"] == "text"


def test_dingtalk_recall_url_selection():
    """Recall endpoint routing: group chats use groupMessages/recall (requires openConversationId); one-on-one chats use batchRecall."""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    group = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                       external_conversation_id="cidG")
    url, body = adapter._recall_url_body(_FakeDTConn(), group, "pqk_1")
    assert url.endswith("/v1.0/robot/groupMessages/recall")
    assert body["openConversationId"] == "cidG" and body["processQueryKeys"] == ["pqk_1"]

    p2p = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="p2p",
                     external_conversation_id="cidP")
    url2, body2 = adapter._recall_url_body(_FakeDTConn(), p2p, "pqk_2")
    assert url2.endswith("/v1.0/robot/otoMessages/batchRecall")
    assert "openConversationId" not in body2

    # Group chat missing openConversationId → None (recall_message returns bad_format based on this)
    orphan = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                        external_conversation_id="")
    assert adapter._recall_url_body(_FakeDTConn(), orphan, "pqk_3") is None
    r = asyncio.run(adapter.recall_message(_FakeDTConn(), orphan, ""))
    assert r.success is False and r.error_kind == "bad_format"


def test_deliver_reply_recalls_placeholder_when_edit_unsupported():
    """Edit failure (DingTalk) → recall the placeholder then send the formal reply, visually equivalent to a "replace"."""
    from core.channels.inbound import _deliver_reply

    calls = {"recalled": [], "md": []}

    class _Adapter:
        class caps:
            supports_markdown = True
            max_message_len = 4000

        @staticmethod
        def prepare_markdown(text):
            return text

        async def edit_message(self, conn, mid, text):
            return SendResult.fail("bad_format", "不支持")

        async def recall_message(self, conn, msg, mid):
            calls["recalled"].append(mid)
            return SendResult.ok(mid)

        async def send_markdown(self, conn, msg, text):
            calls["md"].append(text)
            return SendResult.ok()

        async def send_text(self, conn, msg, text):
            return SendResult.ok()

    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="p2p",
                     external_conversation_id="cid")
    asyncio.run(_deliver_reply(_Adapter(), object(), msg, "正式回复", "pqk_ph"))
    assert calls["recalled"] == ["pqk_ph"]     # placeholder recalled
    assert calls["md"] == ["正式回复"]          # reply sent as a new message


def test_replace_placeholder_falls_back_to_recall_and_send():
    """Error / no-text receipt: edit failure → recall + send anew (the old logic only edited, so DingTalk users would be stuck on "processing" forever)."""
    from core.channels.inbound import _replace_placeholder

    calls = {"recalled": [], "sent": []}

    class _Adapter:
        async def edit_message(self, conn, mid, text):
            return SendResult.fail("bad_format", "不支持")

        async def recall_message(self, conn, msg, mid):
            calls["recalled"].append(mid)
            return SendResult.ok(mid)

        async def send_text(self, conn, msg, text):
            calls["sent"].append(text)
            return SendResult.ok()

    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="p2p",
                     external_conversation_id="cid")
    asyncio.run(_replace_placeholder(_Adapter(), object(), msg, "pqk_ph", "⚠️ 出错了"))
    assert calls["recalled"] == ["pqk_ph"] and calls["sent"] == ["⚠️ 出错了"]


def test_replace_placeholder_without_message_id_sends_terminal_notice_directly():
    """WeChat returns no placeholder ID, but users must still receive the terminal message."""
    from core.channels.inbound import _replace_placeholder

    calls = {"edited": 0, "sent": []}

    class _Adapter:
        async def edit_message(self, conn, mid, text):
            calls["edited"] += 1
            return SendResult.fail("bad_format", "不支持")

        async def send_text(self, conn, msg, text):
            calls["sent"].append(text)
            return SendResult.ok()

    msg = InboundMsg(
        channel_id="c",
        channel_type="weixin",
        text="",
        chat_type="p2p",
        external_conversation_id="wx-user",
    )
    asyncio.run(_replace_placeholder(_Adapter(), object(), msg, None, "⚠️ 处理失败"))
    assert calls == {"edited": 0, "sent": ["⚠️ 处理失败"]}


def test_collect_reply_raises_user_facing_stream_error(monkeypatch):
    from core.channels.inbound import ChannelRunError, _collect_reply
    from orchestration import chat_run_executor

    async def _events():
        yield {"type": "thinking", "delta": "内部思考"}
        yield {"type": "error", "error": "模型暂时不可用"}

    monkeypatch.setattr(chat_run_executor, "follow_run", lambda run_id: _events())

    with pytest.raises(ChannelRunError, match="模型暂时不可用"):
        asyncio.run(_collect_reply("run_failed"))


# ── Channel markdown adaptation (core/channels/markdown.py) ───────────────
def test_markdown_strip_citation_markers():
    from core.channels.markdown import strip_citation_markers

    assert strip_citation_markers("A[ref:internet_search-1]B[ref:query_database-12]") == "AB"
    assert strip_citation_markers("无标记原样") == "无标记原样"
    assert strip_citation_markers("") == ""


def test_markdown_strip_inline_thinking():
    """Inline chain of thought must never reach the channel as message text.
    Semantics mirror the web frontend (segments.ts): body = whatever follows the LAST </think>."""
    from core.channels.markdown import strip_inline_thinking

    # Full tag pair
    assert strip_inline_thinking("<think>推理过程</think>\n最终回复") == "最终回复"
    # Multi-round tool loop: one thinking block per round, opening <think> often absent
    # (server-side template pre-fill) — only the text after the last close tag survives
    assert strip_inline_thinking("先想想A</think>再想想B</think>正式答复") == "正式答复"
    # Dangling opener (stream cut mid-thinking) → drop the unclosed tail
    assert strip_inline_thinking("正文<think>没写完的思考") == "正文"
    # All-thinking reply collapses to empty (caller then sends the no-text receipt)
    assert strip_inline_thinking("<think>只有思考") == ""
    # No tags → untouched
    assert strip_inline_thinking("普通回复") == "普通回复"
    assert strip_inline_thinking("") == ""


def test_collect_reply_strips_inline_thinking(monkeypatch):
    """_collect_reply: content deltas carrying inline <think> spans (channel runs use
    enable_thinking=True, so the streaming layer forwards them raw) must be stripped
    before delivery; thinking events stay ignored as before."""
    from core.channels import inbound as inbound_mod
    from orchestration import chat_run_executor

    events = [
        {"type": "thinking", "delta": "结构化思考,本就不进正文"},
        {"type": "content", "delta": "<think>内联思"},
        {"type": "content", "delta": "考跨 delta</think>你好,"},
        {"type": "content", "delta": "这是答复"},
        {"type": "meta", "artifacts": [{"file_id": "f1"}]},
    ]

    async def _fake_follow(run_id):
        for ev in events:
            yield ev

    monkeypatch.setattr(chat_run_executor, "follow_run", _fake_follow)
    reply, artifacts = asyncio.run(inbound_mod._collect_reply("run_x"))
    assert reply == "你好,这是答复"
    assert artifacts == [{"file_id": "f1"}]


def test_markdown_derive_title():
    from core.channels.markdown import derive_title

    assert derive_title("# 每周产业简报\n正文") == "每周产业简报"
    assert derive_title("**加粗开头** 后续") == "加粗开头 后续"
    assert derive_title("- [官网](https://x.y) 入口") == "官网 入口"
    assert derive_title("\n\n") == "新消息"
    assert len(derive_title("很长" * 40)) == 20


def test_markdown_downgrade_for_dingtalk():
    from core.channels.markdown import downgrade_for_dingtalk

    # Well-formed table → row-style list; supported syntax like headings/lists kept as-is
    md = "# 标题\n| 姓名 | 部门 |\n| --- | :---: |\n| 张三 | 研发 |\n| 李四 | 市场 |\n- 列表项"
    out = downgrade_for_dingtalk(md)
    assert "# 标题" in out and "- 列表项" in out
    assert "- 姓名: 张三｜部门: 研发" in out
    assert "- 姓名: 李四｜部门: 市场" in out
    assert "|" not in out.replace("｜", "")

    # Code fences: fence lines removed, content kept (embedded | lines are not misjudged as tables)
    code = "```python\nprint('hi')\n| not a table\n```\n尾行"
    out2 = downgrade_for_dingtalk(code)
    assert "```" not in out2
    assert "print('hi')" in out2 and "| not a table" in out2 and "尾行" in out2

    # Ill-formed (no separator row) pipe content untouched
    raw = "|a|b|\n|1|2|"
    assert downgrade_for_dingtalk(raw) == raw
    # Idempotent
    assert downgrade_for_dingtalk(out) == out


def test_outbound_synthetic_msg_carries_chat_type_and_peer():
    # The proactively delivered synthetic message must carry the conversation profile — DingTalk selects the robot endpoint by chat_type/sender_id
    from core.channels.outbound import _synthetic_msg

    m = _synthetic_msg("chan_x", "dingtalk", "cidABC", chat_type="p2p", peer_id="staff_9")
    assert m.chat_type == "p2p" and m.sender_id == "staff_9"
    assert m.external_conversation_id == "cidABC"
    # Default is still group (backward compatible with old callers)
    assert _synthetic_msg("chan_x", "lark", "oc_1").chat_type == "group"


# ── WeCom (WeChat Work) adapter ───────────────────────────────────────────
def _wecom_conn(token="tk", aes_key_b64=None):
    """Build a fake conn with real Fernet-encrypted credentials."""
    from core.infra.crypto import encrypt_secret

    class _C:
        channel_id = "chan_wc"
        channel_type = "wecom"
        app_id = "corp1"
        config = {
            "app_secret_enc": encrypt_secret("sec"),
            "agent_id_enc": encrypt_secret("1000002"),
            "token_enc": encrypt_secret(token),
            "aes_key_enc": encrypt_secret(aes_key_b64),
        }
    return _C()


def _wecom_encrypt(key32: bytes, msg: str, receiveid: str = "corp1") -> str:
    import struct
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = msg.encode("utf-8")
    plain = b"0123456789abcdef" + struct.pack(">I", len(body)) + body + receiveid.encode()
    pad = 16 - (len(plain) % 16)
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key32), modes.CBC(key32[:16])).encryptor()
    return base64.b64encode(enc.update(plain) + enc.finalize()).decode()


def test_wecom_signature_pure():
    from core.channels.adapters.wecom import WeComAdapter

    sig = WeComAdapter._signature("tok", "1700000000", "nonce1", "ENC")
    # Matches hand computation: sha1(sorted concatenation)
    import hashlib as _h
    expect = _h.sha1("".join(sorted(["tok", "1700000000", "nonce1", "ENC"])).encode()).hexdigest()
    assert sig == expect


def test_wecom_url_verify_roundtrip_and_signature():
    from core.channels.adapters.wecom import WeComAdapter

    key32 = bytes(range(32))
    aes_key_b64 = base64.b64encode(key32).decode().rstrip("=")  # 43-character EncodingAESKey
    conn = _wecom_conn(token="mytoken", aes_key_b64=aes_key_b64)
    echostr = _wecom_encrypt(key32, "hello-echo")
    sig = WeComAdapter._signature("mytoken", "ts", "nc", echostr)
    params = {"echostr": echostr, "msg_signature": sig, "timestamp": "ts", "nonce": "nc"}
    assert WeComAdapter().verify_url(conn, params) == "hello-echo"
    # Wrong signature → raises
    with pytest.raises(ValueError):
        WeComAdapter().verify_url(conn, {**params, "msg_signature": "bad"})


def test_wecom_decrypt_webhook_and_parse_inbound():
    from core.channels.adapters.wecom import WeComAdapter

    key32 = bytes(range(32))
    aes_key_b64 = base64.b64encode(key32).decode().rstrip("=")
    conn = _wecom_conn(token="mytoken", aes_key_b64=aes_key_b64)
    inner_xml = (
        "<xml><ToUserName><![CDATA[corp1]]></ToUserName>"
        "<FromUserName><![CDATA[userA]]></FromUserName>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[你好]]></Content>"
        "<MsgId>12345</MsgId></xml>"
    )
    encrypt = _wecom_encrypt(key32, inner_xml)
    body = f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode()
    headers = {}
    payload = WeComAdapter().decrypt_webhook(conn, body, headers)
    assert payload["Content"] == "你好"
    assert headers["_encrypt"] == encrypt  # backfilled for signature verification
    msg = WeComAdapter().parse_inbound(conn, payload)
    assert msg.text == "你好"
    assert msg.chat_type == "p2p"
    assert msg.external_conversation_id == "userA"


def test_wecom_edit_message_unsupported():
    from core.channels.adapters.wecom import WeComAdapter

    r = asyncio.run(WeComAdapter().edit_message(_wecom_conn(aes_key_b64="x"), "mid", "x"))
    assert r.success is False


# ── WeChat iLink adapter ────────────────────────────────────────────────────
class _FakeWXConn:
    channel_id = "chan_wx"
    channel_type = "weixin"
    app_id = "wx_abc"
    config = {}


def test_weixin_parse_inbound_text_and_context_token():
    from core.channels.adapters.weixin import WeixinAdapter

    adapter = WeixinAdapter()
    payload = {
        "from_user_id": "o9cq_xxx@im.wechat",
        "message_type": 1,
        "context_token": "CTX-TOKEN-123",
        "item_list": [{"type": 1, "text_item": {"text": "你好微信"}}],
    }
    msg = adapter.parse_inbound(_FakeWXConn(), payload)
    assert msg.text == "你好微信"
    assert msg.chat_type == "p2p"
    assert msg.external_conversation_id == "o9cq_xxx@im.wechat"
    assert msg.raw["weixin_context_token"] == "CTX-TOKEN-123"
    assert msg.raw["weixin_to_user_id"] == "o9cq_xxx@im.wechat"
    # Non-text (image) → None
    assert adapter.parse_inbound(_FakeWXConn(), {"message_type": 2}) is None


def test_weixin_uin_header_is_base64_decimal():
    from core.channels.adapters.weixin import _uin_header

    raw = base64.b64decode(_uin_header()).decode()
    assert raw.isdigit() and 0 <= int(raw) <= 0xFFFFFFFF


def test_weixin_caps_qr_bind_mode():
    from core.channels.adapters.weixin import WeixinAdapter

    assert WeixinAdapter.caps.bind_mode == "qr"
    assert WeixinAdapter.caps.credential_fields == ()


def test_weixin_has_push_file():
    # Inbound orchestration decides whether to return generated files by hasattr(adapter, "push_file") — if missing, it is silently skipped
    from core.channels.adapters.weixin import WeixinAdapter

    assert callable(getattr(WeixinAdapter(), "push_file", None))


def test_weixin_push_file_requires_token_and_target():
    from core.channels.adapters.weixin import WeixinAdapter

    adapter = WeixinAdapter()
    msg = InboundMsg(channel_id="chan_wx", channel_type="weixin", text="",
                     chat_type="p2p", external_conversation_id="o9cq_xxx@im.wechat")
    # No bot_token → forbidden (_FakeWXConn.config is empty)
    r = asyncio.run(adapter.push_file(_FakeWXConn(), msg, b"data", "a.docx", "application/x"))
    assert r.success is False and r.error_kind == "forbidden"


def test_weixin_aes_ecb_encrypt_pkcs7():
    from core.channels.adapters.weixin import _aes_ecb_encrypt

    key = bytes(range(16))
    ct = _aes_ecb_encrypt(b"hello weixin cdn", key)  # 16-byte plaintext → full PKCS7 padding block
    assert len(ct) == 32 and ct != b"hello weixin cdn"
    # Empty content must also be encryptable (1 padding block)
    assert len(_aes_ecb_encrypt(b"", key)) == 16


# ── Registry: all four channels present ────────────────────────────────────
def test_registry_lists_all_four_channels():
    from core.channels.registry import list_adapters

    got = set(list_adapters())
    assert {"lark", "dingtalk", "wecom", "weixin"}.issubset(got)


def test_caps_bind_mode_defaults():
    from core.channels.adapters.lark import LarkAdapter
    from core.channels.adapters.dingtalk import DingTalkAdapter
    from core.channels.adapters.wecom import WeComAdapter

    assert LarkAdapter.caps.bind_mode == "credentials"
    assert DingTalkAdapter.caps.bind_mode == "credentials"
    assert WeComAdapter.caps.credential_fields == ("app_id", "app_secret", "agent_id", "token", "aes_key")


# ── Inbound history loading: preserve cross-turn tool calls/results (regression guard) ──
def test_inbound_history_preserves_tool_calls(db_session):
    """_load_history goes through compaction_service.load_session_history (same
    source as the web UI) and must preserve the assistant turns' tool_calls /
    tool results, instead of the old approach of stripping them to plain text.

    Old bug: channel history kept only user/assistant text → across turns the
    model could not see the tools it had called in its previous turn, redoing
    work repeatedly and spinning idle. Here we assert the tool_call replay
    (role="tool" carrier + tool name) is still present.
    """
    from core.channels.inbound import _load_history
    from core.services.chat_service import ChatService

    _mk_user(db_session, uid="owner1")
    cs = ChatService(db_session)
    session = cs.create_session(user_id="owner1", title="渠道会话")
    chat_id = session.chat_id

    cs.add_message(chat_id=chat_id, role="user", content="生成一份周报")
    cs.add_message(
        chat_id=chat_id, role="assistant", content="好的，已生成。",
        tool_calls=[{
            "tool_name": "word_create", "tool_id": "tc_1", "status": "success",
            "tool_args": {"title": "周报"}, "tool_result": "已生成 weekly.docx",
        }],
    )

    history = _load_history(db_session, chat_id, "owner1")

    # Tool replay is present: the assistant block list carries the tool name, and there is a role="tool" result carrier
    roles = [m["role"] for m in history]
    assert "tool" in roles, f"工具结果载体被丢弃，历史被剥成纯文本: {roles}"
    dumped = json.dumps(history, default=str, ensure_ascii=False)
    assert "word_create" in dumped, "工具调用信息未保留在跨轮历史中"


def test_inbound_history_empty_on_no_access(db_session):
    """When the session does not exist / access is denied, load_session_history returns None → _load_history falls back to an empty list."""
    from core.channels.inbound import _load_history

    _mk_user(db_session, uid="owner2")
    assert _load_history(db_session, "chat_does_not_exist", "owner2") == []


# ── 群聊旁听（observe_all）─────────────────────────────────────────────────────


def test_lark_parse_extracts_mentions_and_defers_group_decision():
    """群消息的 @ 判定必须延后：飞书事件里没有机器人自己的 open_id，只能先收集 mentions。"""
    adapter = LarkAdapter()
    evt = {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "message": {
            "chat_id": "oc_1", "chat_type": "group", "message_id": "om_1",
            "message_type": "text", "content": json.dumps({"text": "@_user_1 帮我看下"}),
            "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "小助手"}],
        },
        "sender": {"sender_id": {"open_id": "ou_a"}}}}
    m = adapter.parse_inbound(_FakeConn(), evt)
    assert m.mentioned_ids == ["ou_bot"]
    assert m.addressed_to_bot is None, "群消息不应在 parse 阶段就下结论"

    # 单聊永远是冲着机器人来的，不需要再解析
    evt["event"]["message"]["chat_type"] = "p2p"
    assert adapter.parse_inbound(_FakeConn(), evt).addressed_to_bot is True


def test_dingtalk_parse_uses_is_in_at_list():
    """钉钉自带 isInAtList，可同步判定；字段缺失时按「已被 @」处理，保持老行为。"""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    base = {"msgtype": "text", "text": {"content": "hi"}, "conversationType": "2",
            "conversationId": "cid_1", "msgId": "m1"}
    assert adapter.parse_inbound(_FakeConn(), {**base, "isInAtList": True}).addressed_to_bot is True
    assert adapter.parse_inbound(_FakeConn(), {**base, "isInAtList": False}).addressed_to_bot is False
    # 未开通群消息读取权限时钉钉不下发该字段 → 不能误判成「没 @ 」而静音
    assert adapter.parse_inbound(_FakeConn(), base).addressed_to_bot is True
    # 单聊不受影响
    assert adapter.parse_inbound(
        _FakeConn(), {**base, "conversationType": "1", "isInAtList": False}
    ).addressed_to_bot is True


def test_resolve_addressed_fails_open(db_session):
    """判定不了就当被 @ 了：宁可多答一句，也不能对着直接 @ 装死。"""
    from core.channels.inbound import _resolve_addressed

    conn = _mk_conn(db_session)
    msg = _inbound("oc_g1", "group")

    class _NoResolver:
        pass

    class _Boom:
        async def resolve_addressed(self, conn, inbound):
            raise RuntimeError("bot/v3/info 挂了")

    class _Says:
        def __init__(self, v): self.v = v
        async def resolve_addressed(self, conn, inbound): return self.v

    # 渠道没实现该钩子（企微/微信）→ 行为与本特性上线前完全一致
    assert asyncio.run(_resolve_addressed(_NoResolver(), conn, msg)) is True
    assert asyncio.run(_resolve_addressed(_Boom(), conn, msg)) is True
    assert asyncio.run(_resolve_addressed(_Says(False), conn, msg)) is False
    # adapter 已给出结论时不再回调钩子
    decided = _inbound("oc_g1", "group")
    decided.addressed_to_bot = True
    assert asyncio.run(_resolve_addressed(_Says(False), conn, decided)) is True


def test_observe_buffer_accumulates_caps_and_drains(db_session):
    """旁听消息进缓冲区、有水位上限、被 @ 时一次性排空。"""
    from core.channels.inbound import (
        _OBSERVE_MAX, _drain_observed, _find_or_create_session, _observe_message,
    )

    conn = _mk_conn(db_session)
    for i in range(_OBSERVE_MAX + 5):
        m = _inbound("oc_g1", "group", text=f"闲聊{i}", mid=f"m{i}")
        m.sender_name = f"甲{i}"
        _observe_message(db_session, conn, m)

    session = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group"))
    buf = (session.extra_data or {}).get("observed")
    assert len(buf) == _OBSERVE_MAX, "旁听缓冲必须有水位上限，否则活跃群会把提示词撑爆"
    assert buf[0]["t"] == "闲聊5", "应淘汰最旧的，保留最近的"
    assert buf[0]["n"] == "甲5"

    drained = _drain_observed(db_session, session)
    assert len(drained) == _OBSERVE_MAX
    db_session.refresh(session)
    assert (session.extra_data or {}).get("observed") == [], "排空后不能重复注入"
    assert _drain_observed(db_session, session) == []


def test_observe_text_truncates_and_keeps_attachment_keys(db_session):
    """旁听正文截断；附件只留句柄不下载，但 key 必须保住——丢了 key 就永远取不回。"""
    from core.channels.inbound import (
        _OBSERVE_TEXT_MAX, _observe_attachment_refs, _observe_text,
    )

    long_msg = _inbound("oc_g1", "group", text="x" * (_OBSERVE_TEXT_MAX + 50))
    assert _observe_text(long_msg) == "x" * _OBSERVE_TEXT_MAX + "…"
    assert _observe_text(_inbound("oc_g1", "group", text="   ")) == ""

    withfile = _inbound("oc_g1", "group", text="看下这个")
    withfile.attachments = [
        {"kind": "file", "key": "fk", "name": "报表.xlsx"},
        {"kind": "image", "key": "ik", "name": "p.png"},
        {"kind": "file", "name": "无 key 的不要"},   # 没有 key 就没法取回，直接丢弃
    ]
    assert _observe_attachment_refs(withfile) == [
        {"kind": "file", "key": "fk", "name": "报表.xlsx"},
        {"kind": "image", "key": "ik", "name": "p.png"},
    ]


def test_observe_records_file_index_for_lazy_fetch(db_session):
    """附件句柄进独立索引（含 message_id / raw），且有水位；纯附件消息也要记。"""
    from core.channels.inbound import (
        _OBSERVE_FILE_INDEX_MAX, _drain_observed, _find_or_create_session, _observe_message,
    )

    conn = _mk_conn(db_session)
    m = _inbound("oc_g1", "group", text="", mid="om_file")
    m.sender_name = "张三"
    m.attachments = [{"kind": "file", "key": "fk_1", "name": "Q3财报.xlsx"}]
    m.raw = {"lark_message_id": "om_file"}
    _observe_message(db_session, conn, m)

    session = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group"))
    idx = (session.extra_data or {})["observed_files"]
    assert idx["fk_1"]["name"] == "Q3财报.xlsx"
    assert idx["fk_1"]["message_id"] == "om_file", "缺 message_id 飞书就取不回资源"
    assert idx["fk_1"]["raw"] == {"lark_message_id": "om_file"}

    # 索引在缓冲区排空后仍然存在——模型可能过几轮才决定要看这个文件
    _drain_observed(db_session, session)
    db_session.refresh(session)
    assert "fk_1" in (session.extra_data or {})["observed_files"]

    # 水位
    for i in range(_OBSERVE_FILE_INDEX_MAX + 3):
        mi = _inbound("oc_g1", "group", text="", mid=f"om_{i}")
        mi.attachments = [{"kind": "file", "key": f"k{i}", "name": f"f{i}.txt"}]
        _observe_message(db_session, conn, mi)
    db_session.refresh(session)
    idx = (session.extra_data or {})["observed_files"]
    assert len(idx) == _OBSERVE_FILE_INDEX_MAX
    assert "fk_1" not in idx and f"k{_OBSERVE_FILE_INDEX_MAX + 2}" in idx


def test_render_observed_block_exposes_attachment_keys():
    """群聊记录里必须带出 key，否则模型看得到文件却拿不到句柄。"""
    from core.channels.inbound import _render_observed_block

    block = _render_observed_block([
        {"n": "张三", "t": "看下这个", "a": [
            {"kind": "file", "key": "fk_1", "name": "Q3财报.xlsx"},
        ]},
        {"n": "李四", "t": "收到"},
    ])
    assert "[文件：Q3财报.xlsx｜key=fk_1]" in block
    assert "channel_read_attachment" in block, "要告诉模型怎么取回"
    assert "李四：收到" in block

    # 没有附件时不必挂取回提示，省 token
    assert "channel_read_attachment" not in _render_observed_block([{"n": "李四", "t": "收到"}])


def test_render_observed_block_marks_as_do_not_reply():
    """群聊记录必须显式标注为背景、不要逐条回复，否则模型会把群消息当成待办清单挨个答。"""
    from core.channels.inbound import _render_observed_block

    block = _render_observed_block([{"n": "张三", "t": "明天几点"}, {"n": "李四", "t": "十点"}])
    assert "张三：明天几点" in block and "李四：十点" in block
    assert "不要逐条回复" in block
    assert block.endswith("\n"), "需与本次消息之间有分隔"


def test_group_listen_mode_validation():
    """observe_all 只能开在平台可能投递非 @ 群消息的渠道上。"""
    from core.services.channel_service import _validate_group_listen

    assert _validate_group_listen("lark", "observe_all") == "observe_all"
    assert _validate_group_listen("dingtalk", "observe_all") == "observe_all"
    assert _validate_group_listen("lark", None) == "mention_only"
    with pytest.raises(BadRequestError):
        _validate_group_listen("lark", "listen_everything")
    with pytest.raises(BadRequestError):
        _validate_group_listen("weixin", "observe_all")


def test_default_group_listen_mode_is_mention_only(db_session):
    """默认必须是 mention_only —— 旁听全群是隐私敏感行为，只能由 owner 显式打开。"""
    conn = _mk_conn(db_session)
    assert conn.group_listen_mode == "mention_only"
    assert bot_to_dict(conn)["group_listen_mode"] == "mention_only"


def test_inbound_gate_routes_bystander_messages(db_session, monkeypatch):
    """闸门路由：未 @ 的群消息在 observe_all 下只落缓冲、在 mention_only 下直接丢弃，
    两种情况都**绝不**起 run、绝不回复——旁听如果会触发回复，机器人会在群里刷屏。"""
    import core.channels.inbound as inbound_mod

    chan_id = _mk_conn(db_session).channel_id
    repo = ChannelConnectionRepository(db_session)
    # 不 patch close：fixture 是共享文件 SQLite + teardown drop_all，压住 close 会留锁
    # 拖垮后续用例建表。但 _process_inbound 内部会 close，而 Session.close() 会把已加载
    # 对象全部 expunge —— 所以下面一律用 channel_id 重新取 conn，不复用旧引用。
    monkeypatch.setattr(inbound_mod, "SessionLocal", lambda: db_session)

    started = []

    class _FakeAdapter:
        caps = None
        async def resolve_addressed(self, conn, inbound): return False
        async def send_text(self, *a, **k):
            started.append("sent"); return SendResult.ok("x")

    monkeypatch.setattr(inbound_mod, "get_adapter", lambda ct: _FakeAdapter())

    def _boom(*a, **k):
        started.append("run")
        raise AssertionError("旁听消息不得触发 agent run")

    monkeypatch.setattr(
        "orchestration.chat_run_executor.start_run", _boom, raising=False
    )

    msg = _inbound("oc_g1", "group", text="今天几点开会", mid="mo1")
    msg.sender_name = "张三"

    # observe_all：进缓冲，不起 run、不回复
    repo.update(chan_id, {"group_listen_mode": "observe_all"})
    asyncio.run(inbound_mod._process_inbound(msg))
    assert started == [], "旁听消息既不应起 run 也不应回复"
    session = inbound_mod._find_or_create_session(db_session, repo.get_by_id(chan_id), msg)
    assert [it["t"] for it in (session.extra_data or {}).get("observed", [])] == ["今天几点开会"]
    assert (session.extra_data or {})["observed"][0]["n"] == "张三"

    # mention_only：直接丢弃，缓冲区不动
    repo.update(chan_id, {"group_listen_mode": "mention_only"})
    asyncio.run(inbound_mod._process_inbound(
        _inbound("oc_g1", "group", text="不该被记下", mid="mo2")
    ))
    # 同样：close() 已把 session 对象 expunge，重新查一次而不是 refresh
    fresh = inbound_mod._find_or_create_session(db_session, repo.get_by_id(chan_id), msg)
    assert [it["t"] for it in (fresh.extra_data or {}).get("observed", [])] == ["今天几点开会"]
    assert started == []


def test_lark_resolve_addressed_matches_bot_open_id(monkeypatch):
    """飞书 @ 判定：只认机器人自己的 open_id。

    覆盖 include_bot 权限（`im:message.group_at_msg.include_bot:readonly`）下的场景——
    群里 @ 的是**别的**机器人时，我们不该抢答；这在闸门上线前是会误答的。
    """
    adapter = LarkAdapter()

    async def _fake_id(conn): return "ou_me"
    monkeypatch.setattr(LarkAdapter, "_bot_open_id", staticmethod(_fake_id))

    def _msg(ids):
        m = _inbound("oc_g1", "group")
        m.mentioned_ids = ids
        return m

    assert asyncio.run(adapter.resolve_addressed(_FakeConn(), _msg(["ou_me"]))) is True
    assert asyncio.run(adapter.resolve_addressed(_FakeConn(), _msg(["ou_other_bot"]))) is False
    assert asyncio.run(adapter.resolve_addressed(_FakeConn(), _msg([]))) is False

    # 取不到自身 open_id（接口抖动 / 空值）→ 一律按「已被 @」处理，不能在群里装死
    async def _boom(conn): raise RuntimeError("bot/v3/info 502")
    monkeypatch.setattr(LarkAdapter, "_bot_open_id", staticmethod(_boom))
    assert asyncio.run(adapter.resolve_addressed(_FakeConn(), _msg(["ou_other"]))) is True

    async def _empty(conn): return ""
    monkeypatch.setattr(LarkAdapter, "_bot_open_id", staticmethod(_empty))
    assert asyncio.run(adapter.resolve_addressed(_FakeConn(), _msg(["ou_other"]))) is True


# ── 钉钉附件接收 / 下载 ────────────────────────────────────────────────────────


def test_dingtalk_extracts_attachments_by_msgtype():
    """钉钉各消息类型的 downloadCode 提取（字段位置依官方「接收的消息类型」）。"""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    a = DingTalkAdapter()
    base = {"conversationType": "1", "conversationId": "cid", "msgId": "m1",
            "robotCode": "ding_robot_1"}

    f = a.parse_inbound(_FakeConn(), {**base, "msgtype": "file", "content": {
        "downloadCode": "dc_1", "fileName": "合同.pdf", "spaceId": "s", "fileId": "fi"}})
    assert f.attachments == [{"kind": "file", "key": "dc_1", "name": "合同.pdf"}]
    # 纯附件、无正文的消息也必须成立——否则钉钉发文件永远收不到
    assert f.text == ""
    assert f.raw["dingtalk_robot_code"] == "ding_robot_1"

    p = a.parse_inbound(_FakeConn(), {**base, "msgtype": "picture", "content": {
        "downloadCode": "dc_2", "pictureDownloadCode": "pdc"}})
    assert p.attachments[0]["kind"] == "image" and p.attachments[0]["key"] == "dc_2"
    # 只给 pictureDownloadCode 时回退取它
    p2 = a.parse_inbound(_FakeConn(), {**base, "msgtype": "picture",
                                       "content": {"pictureDownloadCode": "pdc"}})
    assert p2.attachments[0]["key"] == "pdc"

    r = a.parse_inbound(_FakeConn(), {**base, "msgtype": "richText", "content": {
        "richText": [{"text": "看图"}, {"downloadCode": "dc_3"}]}})
    assert r.text == "看图" and r.attachments[0]["key"] == "dc_3"

    # 字段缺失不能抛异常（payload 规格松散，抛了会打断长连接）
    assert a.parse_inbound(_FakeConn(), {**base, "msgtype": "file", "content": {}}) is None
    assert a.parse_inbound(_FakeConn(), {**base, "msgtype": "file"}) is None


def test_dingtalk_download_resource_two_hop(monkeypatch):
    """downloadCode → downloadUrl → bytes；缺 robotCode 或过期一律返回 None 不抛。"""
    import httpx as _httpx

    from core.channels.adapters.dingtalk import DingTalkAdapter

    a = DingTalkAdapter()
    calls = []

    class _Resp:
        def __init__(self, code=200, payload=None, content=b""):
            self.status_code, self._p, self.content = code, payload or {}, content
            self.text = json.dumps(self._p)
        def json(self): return self._p

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            calls.append(("post", url, json))
            return _Resp(200, {"downloadUrl": "https://dl.example/x.file"})
        async def get(self, url):
            calls.append(("get", url, None))
            return _Resp(200, content=b"PDFBYTES")

    monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: _Client())
    async def _tok(app_id, secret): return "tk"
    monkeypatch.setattr(DingTalkAdapter, "_access_token", staticmethod(_tok))

    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                     external_conversation_id="cid", message_id="m1",
                     raw={"dingtalk_robot_code": "rc_1"})
    got = asyncio.run(a.download_resource(_FakeConn(), msg, {"kind": "file", "key": "dc_1"}))
    assert got == b"PDFBYTES"
    assert calls[0][1].endswith("/v1.0/robot/messageFiles/download")
    assert calls[0][2] == {"downloadCode": "dc_1", "robotCode": "rc_1"}

    # 没有 robotCode 就调不动接口，直接放弃而不是发一个必失败的请求
    nomeat = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                        external_conversation_id="cid", raw={})
    assert asyncio.run(a.download_resource(_FakeConn(), nomeat, {"key": "dc_1"})) is None
    assert asyncio.run(a.download_resource(_FakeConn(), msg, {"key": ""})) is None


# ── channel_read_attachment 工具（按需取回旁听附件）────────────────────────────


def _reg_channel_tool(user_id, chat_id):
    """注册工具并取出被注册的函数。

    注意：register_* 系列拿到的不是 AgentScope 2.0 的 Toolkit，而是鸭子类型的
    ToolCollector（见 core/llm/tool_collector.py）——真 Toolkit 是一次性构造的。
    """
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.channel_attachment_tool import register_channel_attachment

    tc = ToolCollector()
    register_channel_attachment(tc, user_id=user_id, chat_id=chat_id)
    ft = tc._tools.get("channel_read_attachment")
    assert ft is not None, "channel_read_attachment 未注册成功"
    for attr in ("original_func", "func", "_func"):
        fn = getattr(ft, attr, None)
        if callable(fn):
            return fn
    raise AssertionError(f"取不到底层函数，FunctionTool 属性：{dir(ft)}")


def _tool_json(resp):
    block = resp.content[0]
    text = block["text"] if isinstance(block, dict) else getattr(block, "text")
    return json.loads(text)


def test_channel_read_attachment_fetches_and_stores(db_session, monkeypatch):
    """按 key 取回 → 落 Artifact → 返回 file_id 供 read_artifact 读。"""
    from core.channels.inbound import _find_or_create_session, _observe_message

    conn = _mk_conn(db_session)
    m = _inbound("oc_g1", "group", text="", mid="om_f")
    m.attachments = [{"kind": "file", "key": "fk_1", "name": "Q3财报.xlsx"}]
    m.raw = {"lark_message_id": "om_f"}
    _observe_message(db_session, conn, m)
    session = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group"))

    # 只替换 SessionLocal（工具内部是 `from core.db.engine import SessionLocal` 的函数内导入）。
    # 不要去 patch session.close —— fixture 用的是**共享文件** SQLite 且 teardown 会 drop_all，
    # 把 close 变成空操作会让连接一直占着锁，下一个用例的 create_all 就建不出表了。
    # Session.close() 之后仍可继续使用（会开新事务），所以让 `with` 正常关闭即可。
    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: db_session)

    seen = {}

    class _Ad:
        async def download_resource(self, conn, inbound, att):
            seen["message_id"] = inbound.message_id
            seen["raw"] = dict(inbound.raw or {})
            seen["key"] = att.get("key")
            return b"XLSXBYTES"

    monkeypatch.setattr("core.channels.registry.get_adapter", lambda ct: _Ad())

    tool = _reg_channel_tool(str(session.user_id), session.chat_id)
    out = _tool_json(asyncio.run(tool("fk_1")))

    assert "error" not in out, out
    assert out["filename"] == "Q3财报.xlsx" and out["size"] == len(b"XLSXBYTES")
    assert out["file_id"] and "read_artifact" in out["next"]
    # adapter 拿到了取回所需的上下文（飞书按 message_id 定位资源）
    assert seen["key"] == "fk_1" and seen["message_id"] == "om_f"
    assert seen["raw"] == {"lark_message_id": "om_f"}


def test_channel_read_attachment_rejects_unknown_and_foreign(db_session, monkeypatch):
    """未知 key / 越权会话 / 空 key / 非渠道会话 都必须干净报错，不能抛。"""
    from core.channels.inbound import _find_or_create_session, _observe_message

    conn = _mk_conn(db_session)
    m = _inbound("oc_g1", "group", text="", mid="om_f")
    m.attachments = [{"kind": "file", "key": "fk_1", "name": "a.xlsx"}]
    _observe_message(db_session, conn, m)
    session = _find_or_create_session(db_session, conn, _inbound("oc_g1", "group"))

    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: db_session)

    tool = _reg_channel_tool(str(session.user_id), session.chat_id)
    assert "不能为空" in _tool_json(asyncio.run(tool("  ")))["error"]
    assert "未找到" in _tool_json(asyncio.run(tool("no_such_key")))["error"]

    # 别人的会话：拿到 chat_id 也不能越权取走群文件
    other = _reg_channel_tool("someone_else", session.chat_id)
    assert "无权" in _tool_json(asyncio.run(other("fk_1")))["error"]

    # 不在渠道会话里时工具压根没有 chat_id
    assert "渠道会话" in _tool_json(asyncio.run(_reg_channel_tool("u_o", None)("fk_1")))["error"]


# ── 渠道会话身份注入（让 agent 能去拉入群前的群历史）────────────────────────────


def test_inbound_channel_origin_carries_channel_type(db_session, monkeypatch):
    """channel_origin 必须带上 channel_type，否则提示里说不出「哪个平台」、也选不出 CLI。"""
    import core.channels.inbound as inbound_mod

    chan_id = _mk_conn(db_session).channel_id
    monkeypatch.setattr(inbound_mod, "SessionLocal", lambda: db_session)

    captured = {}

    class _Ad:
        caps = None
        async def resolve_addressed(self, conn, inbound): return True
        async def send_text(self, *a, **k): return SendResult.ok("x")
        async def send_placeholder(self, *a, **k): return SendResult.ok("ph")

    monkeypatch.setattr(inbound_mod, "get_adapter", lambda ct: _Ad())
    # 这个扫描器自己开 DB 会话（不走被 patch 的那个），测试库里没建表 → 直接短路掉，
    # 本用例只关心 context 的组装。
    monkeypatch.setattr(
        "core.chat.context.collect_historical_attachments", lambda *a, **k: []
    )

    async def _fake_start_run(**kwargs):
        captured.update(kwargs.get("context") or {})
        raise RuntimeError("stop here — 只验证 context 组装")

    monkeypatch.setattr("orchestration.chat_run_executor.start_run", _fake_start_run)

    asyncio.run(inbound_mod._process_inbound(
        _inbound("oc_g1", "group", text="帮我看看群里发的文件", mid="mco1")
    ))

    origin = captured.get("channel_origin") or {}
    assert origin.get("channel_type") == "lark"
    assert origin.get("chat_type") == "group"
    assert origin.get("conversation_id") == "oc_g1"
    assert origin.get("channel_id") == chan_id


# ── 确定性群历史拉取（代码触发、增量、带预算）──────────────────────────────────


class _HistAdapter:
    """带 fetch_history 的假 adapter；记录每次调用的 since_ms 以验证增量。"""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def fetch_history(self, conn, conversation_id, *, since_ms, limit, newest_first=False):
        self.calls.append({
            "conv": conversation_id, "since_ms": since_ms,
            "limit": limit, "newest_first": newest_first,
        })
        return self.batches.pop(0) if self.batches else []


def _hist(mid, text, ts_ms, name="群友", attachments=None):
    from core.channels.protocol import HistoryItem
    return HistoryItem(
        message_id=mid, sender_name=name, text=text, ts_ms=ts_ms,
        attachments=attachments or [], raw={"lark_message_id": mid},
    )


def test_history_pull_is_incremental_and_dedupes(db_session):
    """第二次拉取必须以游标为下界，且边界重复的消息不能重复入库。"""
    from core.channels.inbound import _find_or_create_session, _pull_history

    conn = _mk_conn(db_session)
    msg = _inbound("oc_g1", "group", mid="m_at1")
    session = _find_or_create_session(db_session, conn, msg)

    # 用真实量级的 epoch ms：游标只进不退，比冷启动窗口更旧的时间戳不会让它倒退
    import time as _t
    t0 = int(_t.time() * 1000) - 3_600_000       # 1 小时前，落在 24h 冷启动窗口内
    ad = _HistAdapter([
        [_hist("h1", "周三评审会", t0), _hist("h2", "地点 302", t0 + 1_000)],
        # 第二批把 h2 又回了一遍（平台边界通常是闭区间）+ 一条新的
        [_hist("h2", "地点 302", t0 + 1_000), _hist("h3", "带上季度数据", t0 + 2_000)],
    ])

    assert asyncio.run(_pull_history(db_session, ad, conn, msg, session)) == 2
    assert ad.calls[0]["since_ms"] > 0, "首次拉取要有冷启动窗口下界，不能从 0 全量拉"
    assert ad.calls[0]["limit"] == 80

    assert asyncio.run(_pull_history(db_session, ad, conn, msg, session)) == 1, "重复的不该再记一次"
    assert ad.calls[1]["since_ms"] == t0 + 1_000, "第二次必须从游标开始，而不是重新拉一遍窗口"

    db_session.refresh(session)
    texts = [it["t"] for it in (session.extra_data or {})["observed"]]
    assert texts == ["周三评审会", "地点 302", "带上季度数据"]


def test_history_pull_respects_budget_and_truncation(db_session):
    """总字数预算与单条截断都要生效——活跃群不能把提示词撑爆。"""
    from core.channels.inbound import (
        _HISTORY_MAX_TOTAL_CHARS, _OBSERVE_TEXT_MAX, _find_or_create_session, _pull_history,
    )

    conn = _mk_conn(db_session)
    msg = _inbound("oc_g2", "group", mid="m_at2")
    session = _find_or_create_session(db_session, conn, msg)

    import time as _t
    _now = int(_t.time() * 1000)
    big = [_hist(f"b{i}", "x" * 5000, _now - 60_000 + i) for i in range(10)]
    ad = _HistAdapter([big])
    n = asyncio.run(_pull_history(db_session, ad, conn, msg, session))

    db_session.refresh(session)
    entries = (session.extra_data or {})["observed"]
    assert n == len(entries)
    assert all(len(e["t"]) <= _OBSERVE_TEXT_MAX + 1 for e in entries), "单条要截断"
    assert sum(len(e["t"]) for e in entries) <= _HISTORY_MAX_TOTAL_CHARS, "总量要卡预算"


def test_history_pull_records_attachment_handles(db_session):
    """历史里的文件同样只留句柄进索引，且带上 message_id 供后续按需取回。"""
    from core.channels.inbound import _find_or_create_session, _pull_history

    conn = _mk_conn(db_session)
    msg = _inbound("oc_g3", "group", mid="m_at3")
    session = _find_or_create_session(db_session, conn, msg)

    ad = _HistAdapter([[
        _hist("hf1", "看下这个", int(__import__("time").time() * 1000) - 60_000,
              attachments=[{"kind": "file", "key": "fk_h", "name": "周报.docx"}]),
    ]])
    asyncio.run(_pull_history(db_session, ad, conn, msg, session))

    db_session.refresh(session)
    idx = (session.extra_data or {})["observed_files"]
    assert idx["fk_h"]["name"] == "周报.docx"
    assert idx["fk_h"]["message_id"] == "hf1"
    assert idx["fk_h"]["raw"] == {"lark_message_id": "hf1"}


def test_history_pull_degrades_and_stops_retrying(db_session):
    """拉取失败绝不能影响回复；连续失败到上限后不再重试，避免每条消息都白等一次超时。"""
    from core.channels.inbound import _HISTORY_MAX_FAILURES, _find_or_create_session, _pull_history

    conn = _mk_conn(db_session)
    msg = _inbound("oc_g4", "group", mid="m_at4")
    session = _find_or_create_session(db_session, conn, msg)

    class _Broken:
        calls = 0
        async def fetch_history(self, conn, conversation_id, *, since_ms, limit, newest_first=False):
            type(self).calls += 1
            raise RuntimeError("接口 500")

    bad = _Broken()
    for _ in range(_HISTORY_MAX_FAILURES + 3):
        assert asyncio.run(_pull_history(db_session, bad, conn, msg, session)) == 0
    assert _Broken.calls == _HISTORY_MAX_FAILURES, "到上限后应停止重试"

    # 渠道没实现该钩子（钉钉当前状态）→ 直接跳过，不报错
    class _NoHook:
        pass
    assert asyncio.run(_pull_history(db_session, _NoHook(), conn, msg, session)) == 0


def test_lark_history_item_parsing():
    """飞书 im/v1/messages 条目 → HistoryItem；已删除/不支持类型要跳过。"""
    item = LarkAdapter._history_item({
        "message_id": "om_1", "msg_type": "text", "create_time": "1700000000000",
        "sender": {"id": "ou_abcdef123"},
        "body": {"content": json.dumps({"text": "@_user_1 周三评审"})},
    })
    assert item.message_id == "om_1" and item.ts_ms == 1700000000000
    assert item.text == "周三评审", "@ 提及应被剥掉"
    assert item.raw == {"lark_message_id": "om_1"}

    f = LarkAdapter._history_item({
        "message_id": "om_2", "msg_type": "file", "create_time": "1",
        "sender": {"id": "ou_x"},
        "body": {"content": json.dumps({"file_key": "fk", "file_name": "a.pdf"})},
    })
    assert f.attachments == [{"kind": "file", "key": "fk", "name": "a.pdf"}]

    assert LarkAdapter._history_item({"message_id": "om_3", "deleted": True}) is None
    assert LarkAdapter._history_item({"message_id": "om_4", "msg_type": "sticker",
                                      "body": {"content": "{}"}}) is None


# ── 钉钉群历史拉取（走官方 dws CLI，复用已有登录态）──────────────────────────


def test_dingtalk_fetch_history_requires_owner():
    """没有 owner 或会话 ID 就直接跳过，不起子进程。"""
    from core.channels.adapters.dingtalk import DingTalkAdapter

    class _C:
        owner_user_id = ""
        config = {}
        app_id = "k"
        channel_id = "c"

    a = DingTalkAdapter()
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []


def test_dingtalk_fetch_history_dws_call_shape(monkeypatch):
    """命令行形状：group / time（本地时间串）/ limit / json，且以 owner 身份运行。"""
    from core.channels.adapters import dingtalk as dt

    seen = {}

    async def _fake_run(user_id, args, timeout=40):
        seen["user_id"] = user_id
        seen["args"] = args
        seen["timeout"] = timeout
        return (json.dumps({"success": True, "result": {"messages": [
            {"msgId": "m1", "senderNick": "张三", "content": "本周进展", "createTime": 1700000000000},
            {"msgId": "m2", "senderNick": "李四", "content": "   "},
        ]}}), "", 0)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _fake_run)

    class _C:
        owner_user_id = "u_owner"
        config = {}
        app_id = "k"
        channel_id = "c"

    items = asyncio.run(
        dt.DingTalkAdapter().fetch_history(_C(), "cid_x", since_ms=1_700_000_000_000, limit=9)
    )
    assert seen["user_id"] == "u_owner", "必须以连接 owner 的身份跑（凭据按 HOME 隔离）"
    args = seen["args"]
    assert args[:3] == ["chat", "message", "list"]
    assert "--group" in args and args[args.index("--group") + 1] == "cid_x"
    assert args[args.index("--limit") + 1] == "9"
    assert "--format" in args and args[args.index("--format") + 1] == "json"
    tval = args[args.index("--time") + 1]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", tval), tval
    assert seen["timeout"] <= 10, "子进程要有独立上限，不能吃满调用方的预算"
    # 空白正文不入库
    assert [(i.message_id, i.sender_name, i.text) for i in items] == [("m1", "张三", "本周进展")]


def test_dingtalk_fetch_history_degrades(monkeypatch):
    """未登录/超时/坏输出一律返回 []，不把异常抛回入站链路。"""
    from core.channels.adapters import dingtalk as dt

    class _C:
        owner_user_id = "u"
        config = {}
        app_id = "k"
        channel_id = "c"

    a = dt.DingTalkAdapter()

    async def _not_logged_in(user_id, args, timeout=40):
        return ("", "未登录，请先执行 dws auth login", 2)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _not_logged_in)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []

    async def _garbage(user_id, args, timeout=40):
        return ("not json at all", "", 0)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _garbage)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []

    async def _boom(user_id, args, timeout=40):
        raise RuntimeError("子进程炸了")

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _boom)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []


def test_dingtalk_parse_dws_history_real_shape():
    """按 dws 线上真实输出解析：openMessageId / sender / createTime 是本地时间**字符串**。

    这三个字段名跟机器人回调那套完全不同，踩过一次：把 createTime 当 epoch ms 解析会
    静默得到 0，游标就永远停在冷启动窗口，每次 @ 都重拉同一个 24 小时。
    """
    from core.channels.adapters.dingtalk import DingTalkAdapter

    p = DingTalkAdapter._parse_dws_history
    items = p({"errorCode": None, "result": {"hasMore": True, "messages": [{
        "content": "@HugAgentOS 你再看看 现在可以查到了吗",
        "createTime": "2026-08-08 21:57:33",
        "openConversationId": "cidaKkLSKoy6sVI9cklpK2LWw==",
        "openMessageId": "msgpUQWt9mBVdjLj788Z/nnww==",
        "sender": "朱路浩",
    }]}})
    assert len(items) == 1
    it = items[0]
    assert it.message_id == "msgpUQWt9mBVdjLj788Z/nnww==", "去重靠它，取错就重复入库"
    assert it.sender_name == "朱路浩", "sender 已是显示名，不该退回「群成员」"
    assert it.ts_ms > 1_700_000_000_000, f"时间戳没解析出来，游标会卡死: {it.ts_ms}"

    # 时间戳容错
    ts = DingTalkAdapter._dws_ts_ms
    assert ts("2026-08-08 21:57:33") > 0
    assert ts(1786197795294) == 1786197795294
    assert ts("1786197795294") == 1786197795294
    assert ts("") == 0 and ts(None) == 0 and ts("不是时间") == 0

    # 外壳兼容 + 失败态
    assert [i.text for i in p({"messages": [{"openMessageId": "2", "content": "b"}]})] == ["b"]
    assert p({"success": False}) == [] and p({"errorCode": "40001"}) == []
    assert p({}) == [] and p(None) == []


def test_history_coldstart_takes_newest_end(db_session):
    """冷启动要取窗口里**最新**的一批，不是最旧的——7 天窗口 + 条数上限下，取旧端等于
    把一周前的闲聊塞进来、而最近几天反而看不到。截断也必须砍旧的那头。"""
    import time as _t

    from core.channels.inbound import (
        _HISTORY_COLDSTART_WINDOW_H, _find_or_create_session, _pull_history,
    )

    conn = _mk_conn(db_session)
    msg = _inbound("oc_cold", "group", mid="m_cold")
    session = _find_or_create_session(db_session, conn, msg)

    now = int(_t.time() * 1000)
    # 单条先被截到 500 字，所以要够多条才会触到 12000 字总预算（约 24 条）
    batch = [_hist(f"c{i}", "x" * 4000, now - i * 60_000, name=f"甲{i:02d}") for i in range(30)]
    ad = _HistAdapter([batch])
    n = asyncio.run(_pull_history(db_session, ad, conn, msg, session))

    assert ad.calls[0]["newest_first"] is True, "首次拉取必须取新端"
    # 冷启动窗口 = 7 天
    assert _HISTORY_COLDSTART_WINDOW_H == 168
    assert now - ad.calls[0]["since_ms"] <= 168 * 3600 * 1000 + 60_000

    db_session.refresh(session)
    entries = (session.extra_data or {})["observed"]
    assert len(entries) == n and n > 0
    names = [e["n"] for e in entries]
    # 保留的是最新的几条（c0 最新），且落库顺序是时间正序
    assert len(entries) < 30, "总预算应当生效，砍掉一部分"
    assert "甲00" in names, f"最新的一条被预算砍掉了: {names}"
    assert "甲29" not in names, f"最旧的一条不该保留（应从旧端砍）: {names}"
    assert names == sorted(names, key=lambda x: -int(x[1:])), f"落库应为时间正序: {names}"


def test_history_incremental_is_not_newest_first(db_session):
    """有游标之后是增量：从游标往后取，方向必须是旧→新，否则会跳过消息。"""
    import time as _t

    from core.channels.inbound import _find_or_create_session, _pull_history

    conn = _mk_conn(db_session)
    msg = _inbound("oc_inc", "group", mid="m_inc")
    session = _find_or_create_session(db_session, conn, msg)
    t0 = int(_t.time() * 1000) - 3_600_000

    ad = _HistAdapter([[_hist("i1", "第一条", t0)], [_hist("i2", "第二条", t0 + 1000)]])
    asyncio.run(_pull_history(db_session, ad, conn, msg, session))
    asyncio.run(_pull_history(db_session, ad, conn, msg, session))

    assert ad.calls[0]["newest_first"] is True, "第一次是冷启动"
    assert ad.calls[1]["newest_first"] is False, "第二次是增量，必须旧→新"
    assert ad.calls[1]["since_ms"] == t0, "增量以游标为下界"


def test_dingtalk_history_extracts_file_handles():
    """dws 把文件消息渲染成**纯文本**，必须把 fileId 解析回附件句柄。

    不解析的话，拉回来的就只是一句「[文件] xx.docx fileId: ...」——模型看得到有文件，
    却没有任何句柄能打开它，正是实测踩到的现象。
    """
    from core.channels.adapters.dingtalk import DingTalkAdapter as D

    raw = ("[文件] 项目周报.docx fileId: X6GRezwJl2LLnwYYTgZMNMzbWdqbropQ "
           "注意：如需下载使用dws drive download命令下载")
    text, refs = D._extract_dws_files(raw)
    assert refs == [{"kind": "file", "key": "X6GRezwJl2LLnwYYTgZMNMzbWdqbropQ",
                     "name": "项目周报.docx"}]
    assert text == "", "匹配掉的整句要清掉，CLI 用法提示不该进提示词"

    img = "[图片] shot.png fileId: ABC123"
    assert D._extract_dws_files(img)[1][0]["kind"] == "image"

    # 普通消息不受影响
    assert D._extract_dws_files("就是一句普通的话") == ("就是一句普通的话", [])

    items = D._parse_dws_history({"result": {"messages": [
        {"openMessageId": "m1", "sender": "朱路浩", "createTime": "2026-08-08 22:03:21",
         "content": raw},
    ]}})
    assert items[0].attachments[0]["key"] == "X6GRezwJl2LLnwYYTgZMNMzbWdqbropQ"
    assert items[0].raw == {"dws_drive": True}, "要标记走钉盘下载而不是机器人 downloadCode"


def test_dingtalk_download_routes_history_files_to_drive(monkeypatch, tmp_path):
    """历史里的文件按 fileId 走 dws drive download；机器人附件仍走 downloadCode。"""
    from core.channels.adapters import dingtalk as dt
    from core.channels.protocol import InboundMsg

    calls = []

    async def _fake_run(user_id, args, timeout=40):
        calls.append(args)
        # 模拟 CLI 写盘
        out_path = args[args.index("--output") + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"DOCXBYTES")
        return ('{"success": true}', "", 0)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _fake_run)

    class _C:
        owner_user_id = "u_owner"
        config = {}
        app_id = "k"
        channel_id = "c"

    a = dt.DingTalkAdapter()
    hist_msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                          external_conversation_id="cid", raw={"dws_drive": True})
    got = asyncio.run(a.download_resource(_C(), hist_msg, {"kind": "file", "key": "FID1"}))
    assert got == b"DOCXBYTES"
    assert calls[0][:2] == ["drive", "download"]
    assert calls[0][calls[0].index("--node") + 1] == "FID1"

    # 临时目录要清干净（下载跑在后端宿主上，不是一次性沙箱）
    import glob as _g
    assert not _g.glob("/tmp/dws_dl_*"), "临时目录未清理"

    # 没有 dws_drive 标记 → 仍走机器人 downloadCode 那条（缺 robotCode 时短路）
    bot_msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                         external_conversation_id="cid", raw={})
    assert asyncio.run(a.download_resource(_C(), bot_msg, {"key": "dc"})) is None
    assert len(calls) == 1, "机器人附件不该走 dws"


def test_dingtalk_dws_download_degrades(monkeypatch):
    """下载失败/未产出文件 → None，且不残留临时目录。"""
    from core.channels.adapters import dingtalk as dt

    async def _fail(user_id, args, timeout=40):
        return ("", "no permission", 1)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _fail)

    class _C:
        owner_user_id = "u"
        config = {}
        app_id = "k"
        channel_id = "c"

    a = dt.DingTalkAdapter()
    assert asyncio.run(a._download_via_dws(_C(), "FID")) is None
    import glob as _g
    assert not _g.glob("/tmp/dws_dl_*")

    class _NoOwner:
        owner_user_id = ""
        config = {}
        app_id = "k"
        channel_id = "c"
    assert asyncio.run(a._download_via_dws(_NoOwner(), "FID")) is None


def test_dingtalk_history_extracts_online_doc_links():
    """钉钉**在线文档**是链接卡片，不是文件——也要解析成句柄，否则模型只看到一条没用的链接。"""
    from core.channels.adapters.dingtalk import DingTalkAdapter as D

    card = (
        "季度工作台账\n创建者：张三\n"
        "![image](https://img.alicdn.com/x.png)\n"
        "[https://alidocs.dingtalk.com/i/nodes/NODEabc123XYZ?corpId=d1]"
        "(https://alidocs.dingtalk.com/i/nodes/NODEabc123XYZ?corpId=d1)"
    )
    text, refs = D._extract_dws_files(card)
    assert refs == [{"kind": "doc", "key": "NODEabc123XYZ",
                     "name": "季度工作台账"}]
    assert text == "", "卡片正文是标题+图标+两遍长 URL，留着纯属浪费提示词"
    # 同一 URL 在卡片里出现两次（显示文本 + 链接），不能记成两个附件
    assert len(refs) == 1


def test_dingtalk_doc_read_surfaces_real_reason(monkeypatch):
    """平台给了明确理由（如跨组织被拒）时必须原样透出，不能笼统说“可能已过期”。

    实测踩到的就是这个：文档属于另一个组织，dws 明确说明了原因，但用户看到的是
    模糊的“统一身份认证”，往完全错误的方向排查。
    """
    from core.channels.adapters import dingtalk as dt
    from core.channels.protocol import ChannelResourceError, InboundMsg

    cross_org = json.dumps({"error": {
        "category": "api",
        "message": "出于安全考虑，不支持跨组织访问数据，该数据属于组织 A，而 MCP 工具配置的组织为 B",
        "server_error_code": "forbidden.accessDenied",
    }}, ensure_ascii=False)

    async def _refused(user_id, args, timeout=40):
        return (cross_org, "", 1)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _refused)

    class _C:
        owner_user_id = "u"
        config = {}
        app_id = "k"
        channel_id = "c"

    a = dt.DingTalkAdapter()
    msg = InboundMsg(channel_id="c", channel_type="dingtalk", text="", chat_type="group",
                     external_conversation_id="cid", raw={})
    try:
        asyncio.run(a.download_resource(_C(), msg, {"kind": "doc", "key": "NODE1"}))
        raise AssertionError("应当抛出带原因的 ChannelResourceError")
    except ChannelResourceError as exc:
        assert "跨组织" in str(exc)

    # 正常读取：返回 markdown 字节
    async def _ok(user_id, args, timeout=40):
        assert args[:2] == ["doc", "read"]
        assert args[args.index("--content-format") + 1] == "markdown"
        return (json.dumps({"content": "# 台账\n第一行"}, ensure_ascii=False), "", 0)

    monkeypatch.setattr("core.services.dingtalk_service._run_dws", _ok)
    got = asyncio.run(a.download_resource(_C(), msg, {"kind": "doc", "key": "NODE1"}))
    assert got.decode("utf-8").startswith("# 台账")

    # 业务错误判定不能误伤正常返回
    assert dt.DingTalkAdapter._dws_business_error('{"result":{"a":1}}') is None
    assert dt.DingTalkAdapter._dws_business_error("not json") is None


def test_channel_read_attachment_names_docs_with_md_extension(db_session, monkeypatch):
    """在线文档取回的是 markdown 正文，落库文件名必须带 .md。

    read_artifact 的解析器是**纯按扩展名**分发的（file_parser.parse_file），标题类文件名
    没有后缀就会被判成"不支持的格式"——文件下下来了却读不了，等于白取。
    """
    from core.channels.inbound import _find_or_create_session, _record_observed_batch

    conn = _mk_conn(db_session)
    msg = _inbound("oc_doc", "group", mid="m_doc")
    session = _find_or_create_session(db_session, conn, msg)
    _record_observed_batch(db_session, session, [], {
        "NODE1": {"name": "季度工作台账", "kind": "doc",
                  "message_id": "m1", "raw": {}},
        "FID2": {"name": "报表.xlsx", "kind": "file", "message_id": "m2", "raw": {}},
    })

    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: db_session)

    class _Ad:
        async def download_resource(self, conn, inbound, att):
            return b"# tai zhang"

    monkeypatch.setattr("core.channels.registry.get_adapter", lambda ct: _Ad())

    tool = _reg_channel_tool(str(session.user_id), session.chat_id)
    doc = _tool_json(asyncio.run(tool("NODE1")))
    assert doc["filename"].endswith(".md"), doc
    assert doc["mime_type"].startswith("text/"), doc

    # 解析器确实认这个名字
    from core.content.file_parser import parse_file
    assert parse_file(b"# tai zhang", doc["filename"]) == "# tai zhang"
    assert parse_file(b"# tai zhang", "季度工作台账") is None, "无后缀本会被判不支持"

    # 普通文件名不受影响，不该被加后缀
    f = _tool_json(asyncio.run(tool("FID2")))
    assert f["filename"] == "报表.xlsx"
