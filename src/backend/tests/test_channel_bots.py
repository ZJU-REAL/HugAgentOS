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


# ── Channel markdown adaptation (core/channels/markdown.py) ───────────────
def test_markdown_strip_citation_markers():
    from core.channels.markdown import strip_citation_markers

    assert strip_citation_markers("A[ref:internet_search-1]B[ref:query_database-12]") == "AB"
    assert strip_citation_markers("无标记原样") == "无标记原样"
    assert strip_citation_markers("") == ""


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
