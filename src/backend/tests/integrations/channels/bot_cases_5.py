from tests.integrations.channels.bot_support import (
    InboundMsg,
    LarkAdapter,
    _HistAdapter,
    _hist,
    _inbound,
    _mk_conn,
    _share_sessions,
    asyncio,
    json,
    re,
)

def test_history_pull_is_incremental_and_dedupes(db_session, monkeypatch):
    """第二次拉取必须以游标为下界，且边界重复的消息不能重复入库。"""
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
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

    assert asyncio.run(_pull_history(ad, conn, msg, session.chat_id)) == 2
    assert ad.calls[0]["since_ms"] > 0, "首次拉取要有冷启动窗口下界，不能从 0 全量拉"
    assert ad.calls[0]["limit"] == 80

    assert asyncio.run(_pull_history(ad, conn, msg, session.chat_id)) == 1, "重复的不该再记一次"
    assert ad.calls[1]["since_ms"] == t0 + 1_000, "第二次必须从游标开始，而不是重新拉一遍窗口"

    db_session.refresh(session)
    texts = [it["t"] for it in (session.extra_data or {})["observed"]]
    assert texts == ["周三评审会", "地点 302", "带上季度数据"]


def test_history_pull_respects_budget_and_truncation(db_session, monkeypatch):
    """总字数预算与单条截断都要生效——活跃群不能把提示词撑爆。"""
    from integrations.channels.inbound_state import HISTORY_MAX_TOTAL_CHARS as _HISTORY_MAX_TOTAL_CHARS, OBSERVE_TEXT_MAX as _OBSERVE_TEXT_MAX
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
    conn = _mk_conn(db_session)
    msg = _inbound("oc_g2", "group", mid="m_at2")
    session = _find_or_create_session(db_session, conn, msg)

    import time as _t
    _now = int(_t.time() * 1000)
    big = [_hist(f"b{i}", "x" * 5000, _now - 60_000 + i) for i in range(10)]
    ad = _HistAdapter([big])
    n = asyncio.run(_pull_history(ad, conn, msg, session.chat_id))

    db_session.refresh(session)
    entries = (session.extra_data or {})["observed"]
    assert n == len(entries)
    assert all(len(e["t"]) <= _OBSERVE_TEXT_MAX + 1 for e in entries), "单条要截断"
    assert sum(len(e["t"]) for e in entries) <= _HISTORY_MAX_TOTAL_CHARS, "总量要卡预算"


def test_history_pull_records_attachment_handles(db_session, monkeypatch):
    """历史里的文件同样只留句柄进索引，且带上 message_id 供后续按需取回。"""
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
    conn = _mk_conn(db_session)
    msg = _inbound("oc_g3", "group", mid="m_at3")
    session = _find_or_create_session(db_session, conn, msg)

    ad = _HistAdapter([[
        _hist("hf1", "看下这个", int(__import__("time").time() * 1000) - 60_000,
              attachments=[{"kind": "file", "key": "fk_h", "name": "周报.docx"}]),
    ]])
    asyncio.run(_pull_history(ad, conn, msg, session.chat_id))

    db_session.refresh(session)
    idx = (session.extra_data or {})["observed_files"]
    assert idx["fk_h"]["name"] == "周报.docx"
    assert idx["fk_h"]["message_id"] == "hf1"
    assert idx["fk_h"]["raw"] == {"lark_message_id": "hf1"}


def test_history_pull_degrades_and_stops_retrying(db_session, monkeypatch):
    """拉取失败绝不能影响回复；连续失败到上限后不再重试，避免每条消息都白等一次超时。"""
    from integrations.channels.inbound_state import HISTORY_MAX_FAILURES as _HISTORY_MAX_FAILURES
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
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
        assert asyncio.run(_pull_history(bad, conn, msg, session.chat_id)) == 0
    assert _Broken.calls == _HISTORY_MAX_FAILURES, "到上限后应停止重试"

    # 渠道没实现该钩子（钉钉当前状态）→ 直接跳过，不报错
    class _NoHook:
        pass
    assert asyncio.run(_pull_history(_NoHook(), conn, msg, session.chat_id)) == 0


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


def test_dingtalk_fetch_history_requires_owner():
    """没有 owner 或会话 ID 就直接跳过，不起子进程。"""
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

    class _C:
        owner_user_id = ""
        config = {}
        app_id = "k"
        channel_id = "c"

    a = DingTalkAdapter()
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []


def test_dingtalk_fetch_history_dws_call_shape(monkeypatch):
    """命令行形状：group / time（本地时间串）/ limit / json，且以 owner 身份运行。"""
    from integrations.channels.adapters import dingtalk as dt

    seen = {}

    async def _fake_run(user_id, args, timeout=40):
        seen["user_id"] = user_id
        seen["args"] = args
        seen["timeout"] = timeout
        return (json.dumps({"success": True, "result": {"messages": [
            {"msgId": "m1", "senderNick": "张三", "content": "本周进展", "createTime": 1700000000000},
            {"msgId": "m2", "senderNick": "李四", "content": "   "},
        ]}}), "", 0)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _fake_run)

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
    from integrations.channels.adapters import dingtalk as dt

    class _C:
        owner_user_id = "u"
        config = {}
        app_id = "k"
        channel_id = "c"

    a = dt.DingTalkAdapter()

    async def _not_logged_in(user_id, args, timeout=40):
        return ("", "未登录，请先执行 dws auth login", 2)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _not_logged_in)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []

    async def _garbage(user_id, args, timeout=40):
        return ("not json at all", "", 0)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _garbage)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []

    async def _boom(user_id, args, timeout=40):
        raise RuntimeError("子进程炸了")

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _boom)
    assert asyncio.run(a.fetch_history(_C(), "cid", since_ms=0, limit=5)) == []


def test_dingtalk_parse_dws_history_real_shape():
    """按 dws 线上真实输出解析：openMessageId / sender / createTime 是本地时间**字符串**。

    这三个字段名跟机器人回调那套完全不同，踩过一次：把 createTime 当 epoch ms 解析会
    静默得到 0，游标就永远停在冷启动窗口，每次 @ 都重拉同一个 24 小时。
    """
    from integrations.channels.adapters.dingtalk import DingTalkAdapter

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


def test_history_coldstart_takes_newest_end(db_session, monkeypatch):
    """冷启动要取窗口里**最新**的一批，不是最旧的——7 天窗口 + 条数上限下，取旧端等于
    把一周前的闲聊塞进来、而最近几天反而看不到。截断也必须砍旧的那头。"""
    import time as _t

    from integrations.channels.inbound_state import HISTORY_COLDSTART_WINDOW_H as _HISTORY_COLDSTART_WINDOW_H
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
    conn = _mk_conn(db_session)
    msg = _inbound("oc_cold", "group", mid="m_cold")
    session = _find_or_create_session(db_session, conn, msg)

    now = int(_t.time() * 1000)
    # 单条先被截到 500 字，所以要够多条才会触到 12000 字总预算（约 24 条）
    batch = [_hist(f"c{i}", "x" * 4000, now - i * 60_000, name=f"甲{i:02d}") for i in range(30)]
    ad = _HistAdapter([batch])
    n = asyncio.run(_pull_history(ad, conn, msg, session.chat_id))

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


def test_history_incremental_is_not_newest_first(db_session, monkeypatch):
    """有游标之后是增量：从游标往后取，方向必须是旧→新，否则会跳过消息。"""
    import time as _t

    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_history import pull_history as _pull_history

    _share_sessions(monkeypatch, db_session)
    conn = _mk_conn(db_session)
    msg = _inbound("oc_inc", "group", mid="m_inc")
    session = _find_or_create_session(db_session, conn, msg)
    t0 = int(_t.time() * 1000) - 3_600_000

    ad = _HistAdapter([[_hist("i1", "第一条", t0)], [_hist("i2", "第二条", t0 + 1000)]])
    asyncio.run(_pull_history(ad, conn, msg, session.chat_id))
    asyncio.run(_pull_history(ad, conn, msg, session.chat_id))

    assert ad.calls[0]["newest_first"] is True, "第一次是冷启动"
    assert ad.calls[1]["newest_first"] is False, "第二次是增量，必须旧→新"
    assert ad.calls[1]["since_ms"] == t0, "增量以游标为下界"


def test_dingtalk_history_extracts_file_handles():
    """dws 把文件消息渲染成**纯文本**，必须把 fileId 解析回附件句柄。

    不解析的话，拉回来的就只是一句「[文件] xx.docx fileId: ...」——模型看得到有文件，
    却没有任何句柄能打开它，正是实测踩到的现象。
    """
    from integrations.channels.adapters.dingtalk import DingTalkAdapter as D

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
    from integrations.channels.adapters import dingtalk as dt
    from integrations.channels.protocol import InboundMsg

    calls = []

    async def _fake_run(user_id, args, timeout=40):
        calls.append(args)
        # 模拟 CLI 写盘
        out_path = args[args.index("--output") + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"DOCXBYTES")
        return ('{"success": true}', "", 0)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _fake_run)

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
