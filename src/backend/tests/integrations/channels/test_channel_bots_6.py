from tests.integrations.channels.bot_support import (
    InboundMsg,
    _inbound,
    _mk_conn,
    _reg_channel_tool,
    _tool_json,
    asyncio,
    json,
)

def test_dingtalk_dws_download_degrades(monkeypatch):
    """下载失败/未产出文件 → None，且不残留临时目录。"""
    from integrations.channels.adapters import dingtalk as dt

    async def _fail(user_id, args, timeout=40):
        return ("", "no permission", 1)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _fail)

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
    from integrations.channels.adapters.dingtalk import DingTalkAdapter as D

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
    from integrations.channels.adapters import dingtalk as dt
    from integrations.channels.protocol import ChannelResourceError, InboundMsg

    cross_org = json.dumps({"error": {
        "category": "api",
        "message": "出于安全考虑，不支持跨组织访问数据，该数据属于组织 A，而 MCP 工具配置的组织为 B",
        "server_error_code": "forbidden.accessDenied",
    }}, ensure_ascii=False)

    async def _refused(user_id, args, timeout=40):
        return (cross_org, "", 1)

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _refused)

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

    monkeypatch.setattr('integrations.dingtalk.service._run_dws', _ok)
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
    from integrations.channels.inbound_sessions import find_or_create_session as _find_or_create_session
    from integrations.channels.inbound_observation import record_observed_batch as _record_observed_batch

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

    monkeypatch.setattr('integrations.channels.registry.get_adapter', lambda ct: _Ad())

    tool = _reg_channel_tool(str(session.user_id), session.chat_id)
    doc = _tool_json(asyncio.run(tool("NODE1")))
    assert doc["filename"].endswith(".md"), doc
    assert doc["mime_type"].startswith("text/"), doc

    # 解析器确实认这个名字
    from core.files.content.file_parser import parse_file
    assert parse_file(b"# tai zhang", doc["filename"]) == "# tai zhang"
    assert parse_file(b"# tai zhang", "季度工作台账") is None, "无后缀本会被判不支持"

    # 普通文件名不受影响，不该被加后缀
    f = _tool_json(asyncio.run(tool("FID2")))
    assert f["filename"] == "报表.xlsx"
