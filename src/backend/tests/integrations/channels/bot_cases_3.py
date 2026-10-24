from tests.integrations.channels.bot_support import (
    InboundMsg,
    LarkAdapter,
    _FakeConn,
    _FakeWXConn,
    _inbound,
    _mk_conn,
    _mk_user,
    _wecom_conn,
    _wecom_encrypt,
    asyncio,
    base64,
    inbound_delivery,
    json,
    pytest,
    subscription,
)

def test_markdown_strip_inline_thinking():
    """Inline chain of thought must never reach the channel as message text.
    Semantics mirror the web frontend (segments.ts): body = whatever follows the LAST </think>."""
    from integrations.channels.markdown import strip_inline_thinking

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
    from integrations.channels import inbound as inbound_mod
    pass

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

    monkeypatch.setattr(subscription, "follow_run", _fake_follow)
    reply, artifacts = asyncio.run(inbound_delivery.collect_reply("run_x"))
    assert reply == "你好,这是答复"
    assert artifacts == [{"file_id": "f1"}]


def test_markdown_derive_title():
    from integrations.channels.markdown import derive_title

    assert derive_title("# 每周产业简报\n正文") == "每周产业简报"
    assert derive_title("**加粗开头** 后续") == "加粗开头 后续"
    assert derive_title("- [官网](https://x.y) 入口") == "官网 入口"
    assert derive_title("\n\n") == "新消息"
    assert len(derive_title("很长" * 40)) == 20


def test_markdown_downgrade_for_dingtalk():
    from integrations.channels.markdown import downgrade_for_dingtalk

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
    from integrations.channels.outbound import _synthetic_msg

    m = _synthetic_msg("chan_x", "dingtalk", "cidABC", chat_type="p2p", peer_id="staff_9")
    assert m.chat_type == "p2p" and m.sender_id == "staff_9"
    assert m.external_conversation_id == "cidABC"
    # Default is still group (backward compatible with old callers)
    assert _synthetic_msg("chan_x", "lark", "oc_1").chat_type == "group"


def test_wecom_signature_pure():
    from integrations.channels.adapters.wecom import WeComAdapter

    sig = WeComAdapter._signature("tok", "1700000000", "nonce1", "ENC")
    # Matches hand computation: sha1(sorted concatenation)
    import hashlib as _h
    expect = _h.sha1("".join(sorted(["tok", "1700000000", "nonce1", "ENC"])).encode()).hexdigest()
    assert sig == expect


def test_wecom_url_verify_roundtrip_and_signature():
    from integrations.channels.adapters.wecom import WeComAdapter

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
    from integrations.channels.adapters.wecom import WeComAdapter

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
    from integrations.channels.adapters.wecom import WeComAdapter

    r = asyncio.run(WeComAdapter().edit_message(_wecom_conn(aes_key_b64="x"), "mid", "x"))
    assert r.success is False


def test_weixin_parse_inbound_text_and_context_token():
    from integrations.channels.adapters.weixin import WeixinAdapter

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
    from integrations.channels.adapters.weixin import _uin_header

    raw = base64.b64decode(_uin_header()).decode()
    assert raw.isdigit() and 0 <= int(raw) <= 0xFFFFFFFF


def test_weixin_caps_qr_bind_mode():
    from integrations.channels.adapters.weixin import WeixinAdapter

    assert WeixinAdapter.caps.bind_mode == "qr"
    assert WeixinAdapter.caps.credential_fields == ()


def test_weixin_has_push_file():
    # Inbound orchestration decides whether to return generated files by hasattr(adapter, "push_file") — if missing, it is silently skipped
    from integrations.channels.adapters.weixin import WeixinAdapter

    assert callable(getattr(WeixinAdapter(), "push_file", None))


def test_weixin_push_file_requires_token_and_target():
    from integrations.channels.adapters.weixin import WeixinAdapter

    adapter = WeixinAdapter()
    msg = InboundMsg(channel_id="chan_wx", channel_type="weixin", text="",
                     chat_type="p2p", external_conversation_id="o9cq_xxx@im.wechat")
    # No bot_token → forbidden (_FakeWXConn.config is empty)
    r = asyncio.run(adapter.push_file(_FakeWXConn(), msg, b"data", "a.docx", "application/x"))
    assert r.success is False and r.error_kind == "forbidden"


def test_weixin_aes_ecb_encrypt_pkcs7():
    from integrations.channels.adapters.weixin import _aes_ecb_encrypt

    key = bytes(range(16))
    ct = _aes_ecb_encrypt(b"hello weixin cdn", key)  # 16-byte plaintext → full PKCS7 padding block
    assert len(ct) == 32 and ct != b"hello weixin cdn"
    # Empty content must also be encryptable (1 padding block)
    assert len(_aes_ecb_encrypt(b"", key)) == 16


def test_registry_lists_all_four_channels():
    from integrations.channels.registry import list_adapters

    got = set(list_adapters())
    assert {"lark", "dingtalk", "wecom", "weixin"}.issubset(got)


def test_caps_bind_mode_defaults():
    from integrations.channels.adapters.dingtalk import DingTalkAdapter
    from integrations.channels.adapters.lark import LarkAdapter
    from integrations.channels.adapters.wecom import WeComAdapter

    assert LarkAdapter.caps.bind_mode == "credentials"
    assert DingTalkAdapter.caps.bind_mode == "credentials"
    assert WeComAdapter.caps.credential_fields == ("app_id", "app_secret", "agent_id", "token", "aes_key")


def test_inbound_history_preserves_tool_calls(db_session):
    """_load_history goes through compaction_service.load_session_history (same
    source as the web UI) and must preserve the assistant turns' tool_calls /
    tool results, instead of the old approach of stripping them to plain text.

    Old bug: channel history kept only user/assistant text → across turns the
    model could not see the tools it had called in its previous turn, redoing
    work repeatedly and spinning idle. Here we assert the tool_call replay
    (role="tool" carrier + tool name) is still present.
    """
    from integrations.channels.inbound_sessions import load_history as _load_history
    from core.chat.service import ChatService

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
    from integrations.channels.inbound_sessions import load_history as _load_history

    _mk_user(db_session, uid="owner2")
    assert _load_history(db_session, "chat_does_not_exist", "owner2") == []


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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.inbound_observation import resolve_addressed as _resolve_addressed

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
    from integrations.channels.inbound_state import OBSERVE_MAX as _OBSERVE_MAX
    from integrations.channels.inbound_observation import drain_observed as _drain_observed, observe_message as _observe_message
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session

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
