from tests.integrations.channels.bot_support import (
    AccessDeniedError,
    BOOL_CAPABILITY_DEFAULTS,
    BadRequestError,
    ChannelConnectionRepository,
    InboundMsg,
    LarkAdapter,
    SendResult,
    _FakeConn,
    _clean_scope,
    _inbound,
    _mk_conn,
    _mk_user,
    _share_sessions,
    channel_service,
    asyncio,
    base64,
    bot_to_dict,
    hashlib,
    inbound_attachments,
    inbound_sessions,
    inbound_state,
    json,
    pytest,
    resolve_capabilities,
)

def test_capability_bit_registered_default_false():
    assert "can_create_channel_bot" in BOOL_CAPABILITY_DEFAULTS
    assert BOOL_CAPABILITY_DEFAULTS["can_create_channel_bot"] is False


def test_capability_personal_override_wins():
    caps = resolve_capabilities({"can_create_channel_bot": True}, {})
    assert caps["can_create_channel_bot"] is True
    caps2 = resolve_capabilities({}, {"can_create_channel_bot": True})  # team default
    assert caps2["can_create_channel_bot"] is True


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


def test_clean_scope():
    assert _clean_scope(None) is None
    assert _clean_scope({}) is None
    assert _clean_scope({"kb_ids": []}) is None
    assert _clean_scope({"kb_ids": ["k1"], "junk": 1}) == {"kb_ids": ["k1"]}
    assert _clean_scope({"skill_ids": [1, 2]}) == {"skill_ids": ["1", "2"]}


def test_create_bot_denied_without_capability(db_session, monkeypatch):
    _mk_user(db_session, "u_deny", meta={})
    _share_sessions(monkeypatch, db_session)
    with pytest.raises(AccessDeniedError):
        asyncio.run(channel_service.create_bot(
            "u_deny", channel_type="lark", app_id="cli_a", app_secret="s",
        ))


def test_create_bot_token_lock(db_session, monkeypatch):
    _mk_user(db_session, "u_ok", meta={"can_create_channel_bot": True})
    # Pre-occupy the same app
    ChannelConnectionRepository(db_session).create({
        "channel_id": "chan_pre", "owner_user_id": "u_ok", "channel_type": "lark",
        "app_id": "cli_dup", "config": {}, "status": "connected",
    })
    _share_sessions(monkeypatch, db_session)
    with pytest.raises(BadRequestError):
        asyncio.run(channel_service.create_bot(
            "u_ok", channel_type="lark", app_id="cli_dup", app_secret="s",
        ))


def test_create_bot_rejects_unknown_channel(db_session, monkeypatch):
    _mk_user(db_session, "u_ch", meta={"can_create_channel_bot": True})
    _share_sessions(monkeypatch, db_session)
    with pytest.raises(BadRequestError):
        asyncio.run(channel_service.create_bot(
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


def test_create_bot_rejects_unknown_agent(db_session, monkeypatch):
    _mk_user(db_session, "u_ag", meta={"can_create_channel_bot": True})
    _share_sessions(monkeypatch, db_session)
    # agent_id pointing to a nonexistent/inaccessible sub-agent → rejected before any network call
    with pytest.raises(BadRequestError):
        asyncio.run(channel_service.create_bot(
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
    from integrations.channels.protocol import chunk_text
    assert chunk_text("", 100) == []
    assert chunk_text("hello", 100) == ["hello"]
    # Over-length → multiple chunks, each within the limit
    parts = chunk_text("x" * 950, 200)
    assert len(parts) == 5 and all(len(p) <= 200 for p in parts)
    # Prefer breaking at a newline
    parts2 = chunk_text("a" * 150 + "\n" + "b" * 50, 200)
    assert parts2[0] == "a" * 150  # cut at the newline, without the b's


def test_speaker_label():
    from integrations.channels.inbound_observation import speaker_label as _speaker_label
    from integrations.channels.protocol import InboundMsg
    m = InboundMsg(channel_id="c", channel_type="lark", text="hi", chat_type="group",
                   external_conversation_id="oc", sender_id="ou_xyz789", sender_name="张三")
    assert _speaker_label(m) == "张三"
    m2 = InboundMsg(channel_id="c", channel_type="lark", text="hi", chat_type="group",
                    external_conversation_id="oc", sender_id="ou_xyz789")
    assert _speaker_label(m2) == "xyz789"  # no name → tail segment of the open_id


def test_conv_lock_serializes_same_conversation():
    """#1: same-conversation locks serialize; different conversations run concurrently."""
    import asyncio

    from integrations.channels import inbound as inb

    order = []

    async def _fake(msg, tag, delay):
        async with inbound_state.conv_locks[inbound_sessions.conv_key(msg)]:
            order.append(f"{tag}-start")
            await asyncio.sleep(delay)
            order.append(f"{tag}-end")

    def _msg(conv):
        from integrations.channels.protocol import InboundMsg
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
    from integrations.channels.adapters.lark import LarkAdapter
    from integrations.channels.outbound import _synthetic_msg
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


def test_inbound_session_keying(db_session):
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
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


def test_inbound_ingest_attachments_stores_artifact(db_session, monkeypatch):
    import asyncio

    from integrations.channels import inbound as inb
    from core.db.models import Artifact

    conn = _mk_conn(db_session)
    _share_sessions(monkeypatch, db_session)

    class _Adapter:
        async def download_resource(self, conn, msg, att):
            return b"hello world bytes"

    # mock storage (avoid real disk writes) + parse_file
    class _Storage:
        def upload_bytes(self, content, key):
            return f"/local/{key}"
    import core.files.storage as cs
    orig_get = cs.get_storage
    cs.get_storage = lambda: _Storage()
    try:
        msg = _inbound("oc_1", "p2p", text="", mid="mf1")
        msg.attachments = [{"kind": "file", "key": "fk_1", "name": "note.txt"}]
        files = asyncio.run(inbound_attachments.ingest_attachments(_Adapter(), conn, "u_o", "chat_x", msg))
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
    from integrations.channels.inbound_sessions import resolve_enabled as _resolve_enabled

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
