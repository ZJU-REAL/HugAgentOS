from tests.integrations.channels.bot_support import (
    InboundMsg,
    SendResult,
    _FakeDTConn,
    asyncio,
    pytest,
    subscription,
)

def test_dingtalk_parse_inbound_group_and_session_webhook():
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

    adapter = DingTalkAdapter()
    p2p = adapter.parse_inbound(_FakeDTConn(), {
        "msgtype": "text", "text": {"content": "hi"}, "conversationType": "1",
        "conversationId": "cidP2P", "msgId": "m2",
    })
    assert p2p.chat_type == "p2p"
    # Images and other non-text → None (v1 is text only)
    assert adapter.parse_inbound(_FakeDTConn(), {"msgtype": "picture"}) is None


def test_dingtalk_edit_message_unsupported():
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

    r = asyncio.run(DingTalkAdapter().edit_message(_FakeDTConn(), "mid", "x"))
    assert r.success is False and r.error_kind == "bad_format"


def test_dingtalk_has_push_file():
    # Inbound orchestration decides whether to return generated files by hasattr(adapter, "push_file") — if missing, it is silently skipped
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

    assert callable(getattr(DingTalkAdapter(), "push_file", None))


def test_dingtalk_robot_send_requires_target():
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

    assert DingTalkAdapter.caps.supports_markdown is True


def test_dingtalk_send_markdown_via_session_webhook(monkeypatch):
    """Reply scenario (with sessionWebhook): msgtype=markdown + title extraction + table downgrade."""
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.inbound_delivery import deliver_reply as _deliver_reply

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.inbound_delivery import deliver_reply as _deliver_reply

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
    from integrations.channels.inbound_delivery import replace_placeholder as _replace_placeholder

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
    from integrations.channels.inbound_delivery import replace_placeholder as _replace_placeholder

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
    from integrations.channels.inbound_delivery import ChannelRunError, collect_reply as _collect_reply
    pass

    async def _events():
        yield {"type": "thinking", "delta": "内部思考"}
        yield {"type": "error", "error": "模型暂时不可用"}

    monkeypatch.setattr(subscription, "follow_run", lambda run_id: _events())

    with pytest.raises(ChannelRunError, match="模型暂时不可用"):
        asyncio.run(_collect_reply("run_failed"))


def test_markdown_strip_citation_markers():
    from integrations.channels.markdown import strip_citation_markers

    assert strip_citation_markers("A[ref:internet_search-1]B[ref:query_database-12]") == "AB"
    assert strip_citation_markers("无标记原样") == "无标记原样"
    assert strip_citation_markers("") == ""
