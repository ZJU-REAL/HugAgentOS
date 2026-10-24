"""Exported Personal Agent files reach the source IM through the durable outbox."""
from types import SimpleNamespace

import pytest

from integrations.channels.protocol import InboundMsg, SendResult
from core.db.models import Artifact, ChatMessage
from personal_agent.channels import channel_bridge
from personal_agent.context import AgentContext
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401


@pytest.fixture
def channel(sessions, monkeypatch):
    monkeypatch.setattr(channel_bridge, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    conn = SimpleNamespace(channel_id="bot", channel_type="lark", owner_user_id="owner")
    monkeypatch.setattr(channel_bridge, "connection", lambda ident: conn)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        svc.identity.agent().config = {**svc.identity.agent().config, "channel_ids": ["bot"]}
        task = svc.tasks.submit("Make a report", "report")["id"]
        user = db.get(ChatMessage, svc.conversation.list()[0]["id"])
        msg = InboundMsg(channel_id="bot", channel_type="lark", text="Make a report",
                         external_conversation_id="conversation", chat_type="p2p")
        user.extra_data = {**user.extra_data, "channel_origin": {
            "channel_id": "bot", "msg": channel_bridge.pack(msg)}}
        for ident, name, mime in [("image", "图表.png", "image/png"),
                                  ("report", "报告.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]:
            db.add(Artifact(artifact_id=ident, user_id="owner", chat_id=svc.identity.agent().chat_id,
                            type="other", title=name, filename=name, size_bytes=4, mime_type=mime,
                            storage_key=ident, extra_data={"source": "ai_generated",
                                "personal_work_item_id": task, "original_filename": name}))
        db.commit()
    sent = []
    class Adapter:
        caps = SimpleNamespace(supports_markdown=False, max_message_len=0)
        async def send_text(self, conn, msg, text):
            sent.append(("text", text))
            return SendResult.ok()
        async def push_file(self, conn, msg, content, name, mime):
            sent.append(("file", name, mime, content, msg.external_conversation_id))
            return SendResult.ok()
    adapter = Adapter()
    monkeypatch.setattr('integrations.channels.registry.get_adapter', lambda kind: adapter)
    monkeypatch.setattr('core.files.storage.get_storage', lambda: SimpleNamespace(download_bytes=lambda key: b"data"))
    return SimpleNamespace(conn=conn, adapter=adapter, sent=sent, task=task)


def queue(sessions, task, text):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        reply = svc.conversation.append(svc.identity.agent(), "assistant", text, task)
        db.commit()
        return reply.message_id


@pytest.mark.parametrize("kind", ["weixin", "lark", "dingtalk", "wecom"])
async def test_exported_image_and_document_sent_once(sessions, channel, kind):
    channel.conn.channel_type = kind
    text = "完成：[图表](/api/files/image) [报告](/api/files/report) [再次下载](/api/files/report)"
    queue(sessions, channel.task, text)
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    assert channel.sent == [
        ("text", text),
        ("file", "图表.png", "image/png", b"data", "conversation"),
        ("file", "报告.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"data", "conversation"),
    ]


async def test_partial_file_failure_retries_only_unconfirmed_file(sessions, channel):
    attempts = []
    async def push(conn, msg, content, name, mime):
        attempts.append(name)
        return SendResult.fail("transient") if name == "报告.xlsx" and attempts.count(name) == 1 else SendResult.ok()
    channel.adapter.push_file = push
    queue(sessions, channel.task, "[图表](/api/files/image) [报告](/api/files/report)")
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    assert attempts == ["图表.png", "报告.xlsx", "报告.xlsx"]
    assert len(channel.sent) == 1  # Confirmed text is not repeated.


async def test_failed_text_does_not_send_files_or_mark_success(sessions, channel):
    count = 0
    async def send(conn, msg, text):
        nonlocal count
        count += 1
        return SendResult.fail("transient")
    channel.adapter.send_text = send
    queue(sessions, channel.task, "[图表](/api/files/image)")
    for _ in range(7):
        await channel_bridge.deliver_pending()
    assert count == channel_bridge.MAX_ATTEMPTS
    assert channel.sent == []


@pytest.mark.parametrize("revocation", ["rerouted", "reassigned", "capability"])
async def test_channel_authority_is_rechecked_at_dispatch(sessions, channel, monkeypatch, revocation):
    queue(sessions, channel.task, "[图表](/api/files/image)")
    if revocation == "rerouted":
        with sessions() as db:
            svc = AgentContext(db, "owner")
            svc.identity.agent().config = {**svc.identity.agent().config, "channel_ids": []}
            db.commit()
    elif revocation == "reassigned":
        channel.conn.owner_user_id = "other"
    else:
        monkeypatch.setattr(channel_bridge.access, "allowed", lambda *args: False)
    await channel_bridge.deliver_pending()
    assert channel.sent == []


async def test_concurrent_dispatch_and_expired_claim_preserve_receipts(sessions, channel, monkeypatch):
    import asyncio
    import time
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def push(conn, msg, content, name, mime):
        calls.append(name)
        entered.set()
        await release.wait()
        return SendResult.ok()
    channel.adapter.push_file = push
    queue(sessions, channel.task, "[图表](/api/files/image)")
    first = asyncio.create_task(channel_bridge.deliver_pending())
    await entered.wait()
    try:
        second = asyncio.create_task(channel_bridge.deliver_pending())
        await asyncio.sleep(0.05)
        assert calls == ["图表.png"]
    finally:
        first.cancel()
        second.cancel()
        for task in (first, second):
            try: await task
            except asyncio.CancelledError: pass
    release.set()
    now = time.time()
    monkeypatch.setattr(channel_bridge.time, "time", lambda: now + 1000)
    await channel_bridge.deliver_pending()
    assert calls == ["图表.png", "图表.png"]  # Interrupted upload had no confirmed receipt.
    assert len(channel.sent) == 1  # Text receipt survived restart.


@pytest.mark.parametrize("change", ["foreign_user", "other_chat", "deleted", "upload", "oversize", "foreign_task"])
async def test_non_export_or_inaccessible_artifacts_are_not_sent(sessions, channel, change):
    from core.infra.time import utc_now
    with sessions() as db:
        item = db.get(Artifact, "image")
        if change == "foreign_user": item.user_id = "other"
        elif change == "other_chat": item.chat_id = AgentContext(db, "other").identity.ensure()["chat_id"]
        elif change == "deleted": item.deleted_at = utc_now()
        elif change == "upload": item.extra_data = {"source": "user_upload"}
        elif change == "oversize": item.size_bytes = 21 * 1024 * 1024
        else:
            other = AgentContext(db, "other").tasks.submit("Other task", "other")["id"]
            item.extra_data = {**item.extra_data, "personal_work_item_id": other}
        db.commit()
    queue(sessions, channel.task, "[图表](/api/files/image) [外链](https://example.test/image.png)")
    await channel_bridge.deliver_pending()
    assert len(channel.sent) == 1 and channel.sent[0][0] == "text"


async def test_partial_text_failure_does_not_repeat_confirmed_chunks(sessions, channel):
    channel.adapter.caps.max_message_len = 5
    sent = []
    async def send(conn, msg, text):
        sent.append(text)
        return SendResult.fail("transient") if len(sent) == 2 else SendResult.ok()
    channel.adapter.send_text = send
    queue(sessions, channel.task, "1234567890")
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    assert sent == ["12345", "67890", "67890"]


async def test_artifact_deleted_after_queue_is_not_read(sessions, channel, monkeypatch):
    from core.infra.time import utc_now
    queue(sessions, channel.task, "[图表](/api/files/image)")
    with sessions() as db:
        db.get(Artifact, "image").deleted_at = utc_now()
        db.commit()
    def unexpected_storage():
        pytest.fail("Deleted artifact must not reach storage")
    monkeypatch.setattr('core.files.storage.get_storage', unexpected_storage)
    await channel_bridge.deliver_pending()
    assert len(channel.sent) == 1 and channel.sent[0][0] == "text"


async def test_scheduled_output_reaches_private_home_conversation(sessions, channel):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.agent().config = {**svc.identity.agent().config, "heartbeat": {"channel_id": "bot"}}
        # An unattended result has no inbound origin of its own.
        row = svc.conversation.append(svc.identity.agent(), "assistant", "[图表](/api/files/image)", None)
        channel_bridge.mark_for_delivery(db, row, home=True)
        db.commit()
    await channel_bridge.deliver_pending()
    assert channel.sent[-1] == ("file", "图表.png", "image/png", b"data", "conversation")


async def test_bare_relative_download_url_also_sends_file(sessions, channel):
    queue(sessions, channel.task, "下载地址：/api/files/image")
    await channel_bridge.deliver_pending()
    assert channel.sent[-1] == ("file", "图表.png", "image/png", b"data", "conversation")


async def test_invalidated_file_does_not_block_remaining_outputs(sessions, channel):
    from core.infra.time import utc_now
    ident = queue(sessions, channel.task, "[图表](/api/files/image) [报告](/api/files/report)")
    with sessions() as db:
        db.get(Artifact, "image").deleted_at = utc_now()
        db.commit()
    for _ in range(6):
        await channel_bridge.deliver_pending()
    assert [sent[1] for sent in channel.sent if sent[0] == "file"] == ["报告.xlsx"]
    with sessions() as db:
        delivery = db.get(ChatMessage, ident).extra_data["channel_delivery"]
        assert delivery["status"] == "failed"


async def test_simultaneous_sqlite_claims_only_send_one_copy(sessions, channel, monkeypatch, tmp_path):
    import asyncio
    import sqlite3
    import threading
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    queue(sessions, channel.task, "[图表](/api/files/image)")
    path = tmp_path / "concurrent-outbox.sqlite"
    with sqlite3.connect(path) as target:
        source = sessions.kw["bind"].raw_connection()
        try: source.driver_connection.backup(target)
        finally: source.close()
    engine = create_engine("sqlite:///" + str(path))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(channel_bridge, "SessionLocal", factory)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", factory)
    barrier, guard = threading.Barrier(2), threading.Lock()
    reads = 0

    @event.listens_for(engine, "after_cursor_execute")
    def race(conn, cursor, statement, parameters, context, executemany):
        nonlocal reads
        if statement.startswith("SELECT") and "WHERE chat_messages.message_id =" in statement:
            with guard:
                reads += 1
                synchronize = reads <= 2
            if synchronize: barrier.wait(timeout=5)
    try:
        await asyncio.gather(channel_bridge.deliver_pending(), channel_bridge.deliver_pending())
        assert [sent[0] for sent in channel.sent] == ["text", "file"]
    finally:
        engine.dispose()
