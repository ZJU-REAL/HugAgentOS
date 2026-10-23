from tests.integrations.channels.bot_support import (
    _share_sessions,
    BadRequestError,
    ChannelConnectionRepository,
    InboundMsg,
    LarkAdapter,
    SendResult,
    _FakeConn,
    _inbound,
    _mk_conn,
    _reg_channel_tool,
    _tool_json,
    asyncio,
    bot_to_dict,
    inbound,
    inbound_sessions,
    json,
    pytest,
)

def test_observe_text_truncates_and_keeps_attachment_keys(db_session):
    """旁听正文截断；附件只留句柄不下载，但 key 必须保住——丢了 key 就永远取不回。"""
    from integrations.channels.inbound_state import OBSERVE_TEXT_MAX as _OBSERVE_TEXT_MAX
    from integrations.channels.inbound_observation import observe_attachment_refs as _observe_attachment_refs, observe_text as _observe_text

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
    from integrations.channels.inbound_state import OBSERVE_FILE_INDEX_MAX as _OBSERVE_FILE_INDEX_MAX
    from integrations.channels.inbound_observation import drain_observed as _drain_observed, observe_message as _observe_message
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session

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
    from integrations.channels.inbound_observation import render_observed_block as _render_observed_block

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
    from integrations.channels.inbound_observation import render_observed_block as _render_observed_block

    block = _render_observed_block([{"n": "张三", "t": "明天几点"}, {"n": "李四", "t": "十点"}])
    assert "张三：明天几点" in block and "李四：十点" in block
    assert "不要逐条回复" in block
    assert block.endswith("\n"), "需与本次消息之间有分隔"


def test_group_listen_mode_validation():
    """observe_all 只能开在平台可能投递非 @ 群消息的渠道上。"""
    from integrations.channels.service import _validate_group_listen

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
    import integrations.channels.inbound as inbound_mod

    chan_id = _mk_conn(db_session).channel_id
    repo = ChannelConnectionRepository(db_session)
    # 不 patch close：fixture 是共享文件 SQLite + teardown drop_all，压住 close 会留锁
    # 拖垮后续用例建表。但 _process_inbound 内部会 close，而 Session.close() 会把已加载
    # 对象全部 expunge —— 所以下面一律用 channel_id 重新取 conn，不复用旧引用。
    _share_sessions(monkeypatch, db_session)

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
        'core.chat.runtime.admission.start_run', _boom, raising=False
    )

    msg = _inbound("oc_g1", "group", text="今天几点开会", mid="mo1")
    msg.sender_name = "张三"

    # observe_all：进缓冲，不起 run、不回复
    repo.update(chan_id, {"group_listen_mode": "observe_all"})
    asyncio.run(inbound.process_inbound(msg))
    assert started == [], "旁听消息既不应起 run 也不应回复"
    session = inbound_sessions.find_or_create_session(db_session, repo.get_by_id(chan_id), msg)
    assert [it["t"] for it in (session.extra_data or {}).get("observed", [])] == ["今天几点开会"]
    assert (session.extra_data or {})["observed"][0]["n"] == "张三"

    # mention_only：直接丢弃，缓冲区不动
    repo.update(chan_id, {"group_listen_mode": "mention_only"})
    asyncio.run(inbound.process_inbound(
        _inbound("oc_g1", "group", text="不该被记下", mid="mo2")
    ))
    # 同样：close() 已把 session 对象 expunge，重新查一次而不是 refresh
    fresh = inbound_sessions.find_or_create_session(db_session, repo.get_by_id(chan_id), msg)
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


def test_dingtalk_extracts_attachments_by_msgtype():
    """钉钉各消息类型的 downloadCode 提取（字段位置依官方「接收的消息类型」）。"""
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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


def test_channel_read_attachment_fetches_and_stores(db_session, monkeypatch):
    """按 key 取回 → 落 Artifact → 返回 file_id 供 read_artifact 读。"""
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_observation import observe_message as _observe_message

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

    monkeypatch.setattr('integrations.channels.registry.get_adapter', lambda ct: _Ad())

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
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_observation import observe_message as _observe_message

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


def test_inbound_channel_origin_carries_channel_type(db_session, monkeypatch):
    """channel_origin 必须带上 channel_type，否则提示里说不出「哪个平台」、也选不出 CLI。"""
    import integrations.channels.inbound as inbound_mod

    chan_id = _mk_conn(db_session).channel_id
    _share_sessions(monkeypatch, db_session)

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

    monkeypatch.setattr('core.chat.runtime.admission.start_run', _fake_start_run)

    asyncio.run(inbound.process_inbound(
        _inbound("oc_g1", "group", text="帮我看看群里发的文件", mid="mco1")
    ))

    origin = captured.get("channel_origin") or {}
    assert origin.get("channel_type") == "lark"
    assert origin.get("chat_type") == "group"
    assert origin.get("conversation_id") == "oc_g1"
    assert origin.get("channel_id") == chan_id
