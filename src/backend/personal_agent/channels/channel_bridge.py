"""IM channels (DingTalk, Lark, WeChat, WeCom) as another entry to the one Personal Agent conversation.

Inbound: a message from a bot that the owner gave to Personal Agent goes into the normal
message inbox. Outbound: an assistant reply for that message, or for a task it created,
is sent back to the same IM conversation through a durable outbox on the message row.
"""
import asyncio
import dataclasses
import logging
import posixpath
import re
import time
import uuid

from sqlalchemy import and_, or_, select, update

from core.db.engine import SessionLocal
from core.db.threads import run_in_session
from core.db.models import Artifact, ChatMessage
from personal_agent import access
from personal_agent.models import PersonalAgent, PersonalMessageTask

log = logging.getLogger(__name__)
SUPPORTED = ("dingtalk", "lark", "weixin", "wecom")
MAX_ATTEMPTS = 5
DELIVERY_LEASE_SEC = 180
SEND_TIMEOUT_SEC = 60
MAX_FILE_BYTES = 20 * 1024 * 1024
FILE_LINK = re.compile(r"(?<![A-Za-z0-9_:/.-])/(?:api/)?files/([A-Za-z0-9_-]+)(?![A-Za-z0-9_/-])")

class OutputUnavailable(ValueError):
    """A deleted, forbidden or oversized output cannot be retried."""

def exported_artifact(db, chat_id, artifact_id):
    """Resolve only this owner's Personal Agent outputs; never fetch reply URLs."""
    from personal_agent.conversation.conversations import MARKER
    from core.db.models import ChatSession
    session = db.get(ChatSession, chat_id)
    agent = (db.get(PersonalAgent, (session.extra_data or {}).get(MARKER)) if session and (session.extra_data or {}).get(MARKER)
             else db.scalar(select(PersonalAgent).where(PersonalAgent.chat_id == chat_id)))
    item = db.get(Artifact, artifact_id)
    if not agent or not item or item.user_id != agent.user_id or item.chat_id != chat_id:
        return None
    meta = item.extra_data or {}
    from personal_agent.models import PersonalWorkItem
    task = db.get(PersonalWorkItem, meta.get("personal_work_item_id")) if meta.get("personal_work_item_id") else None
    if (item.deleted_at or not item.storage_key or item.size_bytes > MAX_FILE_BYTES
            or meta.get("source") != "ai_generated" or not task or task.agent_id != agent.id):
        return None
    return item

def load_file(chat_id, artifact_id):
    from core.files.storage import get_storage
    with SessionLocal() as db:
        item = exported_artifact(db, chat_id, artifact_id)
        if item is None:
            raise OutputUnavailable("Personal Agent output is no longer available")
        key, mime = item.storage_key, item.mime_type or "application/octet-stream"
        name = posixpath.basename((item.extra_data or {}).get("original_filename") or item.filename)
    content = get_storage().download_bytes(key)
    if len(content) > MAX_FILE_BYTES:
        raise OutputUnavailable("Personal Agent output exceeds 20 MiB")
    return content, name, mime

def handled_by_agent(db, conn):
    """True when the bot owner routed this bot to Personal Agent."""
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == conn.owner_user_id))
    # Without the permission the bot answers as an ordinary channel bot again.
    return bool(agent and conn.channel_id in (agent.config or {}).get("channel_ids", [])
                and access.allowed(db, conn.owner_user_id))

def pack(msg):
    data = dataclasses.asdict(msg)
    data.pop("raw", None)
    return data

def unpack(data):
    from integrations.channels.protocol import InboundMsg
    fields = {field.name for field in dataclasses.fields(InboundMsg)}
    return InboundMsg(**{key: value for key, value in data.items() if key in fields})

def _channel_chat(db, conn, msg):
    """The Personal Agent conversation this IM conversation feeds."""
    from personal_agent.context import AgentContext
    from personal_agent.conversation.conversations import channel_session
    from personal_agent.identity.config import locked_agent
    svc = AgentContext(db, conn.owner_user_id)
    svc.identity.ensure()
    chat_id = channel_session(db, locked_agent(db, svc.user_id), conn, msg).chat_id
    db.commit()
    return chat_id

def _admit_message(db, owner_id, chat_id, text, key, file_ids, origin):
    from personal_agent.context import AgentContext
    from personal_agent.conversation.message_routing import admit
    admit(AgentContext(db, owner_id, chat_id), text, key, file_ids,
          extra={"origin": "channel", "channel_origin": origin})

async def admit_inbound(conn, msg, adapter):
    """Store the IM message in the Personal Agent conversation. The worker answers it later."""
    from integrations.channels.inbound_attachments import ingest_attachments as _ingest_attachments
    from integrations.channels.inbound_observation import speaker_label as _speaker_label
    placeholder = None
    try:
        send = getattr(adapter, "send_placeholder", None) or adapter.send_text
        sent = await send(conn, msg, "🤔 已收到，正在处理…")
        placeholder = sent.message_id if sent.success else None
    except Exception:  # noqa: BLE001 - a missing acknowledgement must not drop the message
        log.debug("Personal channel acknowledgement failed", exc_info=True)
    chat_id = await run_in_session(_channel_chat, conn, msg)
    files = await _ingest_attachments(adapter, conn, conn.owner_user_id, chat_id, msg)
    text = (msg.text or "").strip() or ("[收到文件] " + "、".join(item.get("name", "") for item in files))
    if msg.chat_type == "group": text = _speaker_label(msg) + "：" + text
    key = "channel:" + conn.channel_id + ":" + (msg.message_id or uuid.uuid4().hex)
    origin = {"channel_id": conn.channel_id, "channel_type": conn.channel_type, "msg": pack(msg), "placeholder_id": placeholder}
    await run_in_session(_admit_message, conn.owner_user_id, chat_id, text[:20000], key[:100],
                         [item["file_id"] for item in files if item.get("file_id")][:10], origin)

def origin_of(db, task_id=None, message_id=None):
    """The IM conversation that started a task, or that sent one message."""
    if task_id and not message_id:
        message_id = db.scalar(select(PersonalMessageTask.message_id).where(
            PersonalMessageTask.work_item_id == task_id, PersonalMessageTask.relation == "goal").limit(1))
    if not message_id: return None
    row = db.get(ChatMessage, message_id)
    return (row.extra_data or {}).get("channel_origin") if row else None

def home_origin(db, chat_id):
    """The direct IM conversation of the bot the owner chose for messages nobody asked for
    (heartbeat alerts, scheduled results): the latest one-to-one message from that bot. None without one."""
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.chat_id == chat_id))
    config = (agent.config or {}) if agent else {}
    channel = (config.get("heartbeat") or {}).get("channel_id")
    if not channel or channel not in config.get("channel_ids", []): return None
    from core.db.models import ChatSession
    from personal_agent.conversation.conversations import MARKER
    rows = db.scalars(select(ChatMessage).join(ChatSession, ChatSession.chat_id == ChatMessage.chat_id).where(
        ChatSession.user_id == agent.user_id, ChatMessage.role == "user",
        or_(ChatMessage.chat_id == chat_id, ChatSession.extra_data[MARKER].as_string() == agent.id))
        .order_by(ChatMessage.created_at.desc(), ChatMessage.message_id.desc()).limit(200)).all()
    for row in rows:
        origin = (row.extra_data or {}).get("channel_origin") or {}
        # Never a group: an alert is for the owner only.
        if origin.get("channel_id") == channel and (origin.get("msg") or {}).get("chat_type") != "group": return origin
    return None

def mark_for_delivery(db, message, task_id=None, reply_to=None, home=False):
    """Queue one assistant message for the IM conversation that started it, or for the home bot."""
    origin = origin_of(db, task_id, reply_to)
    if not origin and home: origin = home_origin(db, message.chat_id)
    if origin:
        metadata = message.extra_data or {}
        candidates = (metadata.get("artifact_ids", []) if metadata.get("delivery")
                      else FILE_LINK.findall(message.content or ""))
        file_ids = []
        for ident in dict.fromkeys(candidates):
            item = exported_artifact(db, message.chat_id, ident)
            # A finish/report may mention an already delivered file; its original
            # delivery message alone owns the native attachment send.
            owner = (item.extra_data or {}).get("delivery_message_id") if item else None
            if item and (not owner or owner == message.message_id): file_ids.append(ident)
        message.extra_data = {**(message.extra_data or {}), "channel_delivery": {
            "origin": origin, "status": "pending", "attempts": 0, "file_ids": file_ids}}

async def send_now(origin, text):
    """Best-effort immediate notice, for example an approval that waits in the web page."""
    conn = await asyncio.to_thread(connection, origin["channel_id"])
    if conn is None: return False
    return await send_text(conn, unpack(origin["msg"]), text)

async def send_text(conn, msg, text, sent_chunks=0, record=None):
    """Persist each successful chunk before attempting the next one."""
    from integrations.channels.markdown import strip_citation_markers, strip_inline_thinking
    from integrations.channels.protocol import chunk_text
    from integrations.channels.registry import get_adapter
    adapter = get_adapter(conn.channel_type)
    text = strip_citation_markers(strip_inline_thinking(text))
    send = getattr(adapter, "send_markdown", None) if adapter.caps.supports_markdown else None
    prepare = getattr(adapter, "prepare_markdown", None)
    if send and callable(prepare): text = prepare(text)
    chunks = chunk_text(text, adapter.caps.max_message_len)
    for index, chunk in enumerate(chunks):
        if index < sent_chunks: continue
        if record: await record()
        result = await (send or adapter.send_text)(conn, msg, chunk)
        if not result.success: return False
        if record: await record(text_chunks_sent=index + 1)
    return True

def delivery_allowed(chat_id, conn):
    with SessionLocal() as db:
        from personal_agent.conversation.conversations import MARKER
        from core.db.models import ChatSession
        session = db.get(ChatSession, chat_id)
        agent = (db.get(PersonalAgent, (session.extra_data or {}).get(MARKER)) if session and (session.extra_data or {}).get(MARKER)
                 else db.scalar(select(PersonalAgent).where(PersonalAgent.chat_id == chat_id)))
        return bool(agent and conn and agent.user_id == conn.owner_user_id
                    and conn.channel_id in (agent.config or {}).get("channel_ids", [])
                    and access.allowed(db, agent.user_id))

def connection(channel_id):
    """An enabled bot connection, detached with its credentials loaded, or None."""
    from integrations.channels.repository import ChannelConnectionRepository
    with SessionLocal() as db:
        conn = ChannelConnectionRepository(db).get_by_id(channel_id)
        if conn is None or not conn.enabled: return None
        _ = conn.config
        db.expunge(conn)
        return conn

class DeliveryLost(RuntimeError):
    """A new worker owns this outbox row; the old worker must stop."""

def save_delivery(db, row, delivery):
    # SQLite ignores FOR UPDATE. CAS also fences simultaneous desktop workers and
    # prevents replacing unrelated metadata written since this row was read (#243).
    changed = db.execute(update(ChatMessage).where(
        ChatMessage.message_id == row.message_id, ChatMessage.extra_data == row.extra_data
    ).values(extra_data={**row.extra_data, "channel_delivery": delivery})
      .execution_options(synchronize_session=False)).rowcount
    if changed != 1: raise DeliveryLost()
    db.commit()

def claim_delivery(ident):
    """Claim under a row lock so independent backend workers cannot both send."""
    with SessionLocal() as db:
        row = db.scalar(select(ChatMessage).where(ChatMessage.message_id == ident).with_for_update())
        if row is None: return None
        delivery = dict((row.extra_data or {}).get("channel_delivery") or {})
        now = time.time()
        if delivery.get("status") != "pending" and not (
                delivery.get("status") == "sending" and delivery.get("lease_until", 0) < now):
            return None
        if delivery.get("attempts", 0) >= MAX_ATTEMPTS:
            delivery["status"] = "failed"
            save_delivery(db, row, delivery)
            return None
        delivery.update(status="sending", token=uuid.uuid4().hex, lease_until=now + DELIVERY_LEASE_SEC,
                        attempts=delivery.get("attempts", 0) + 1)
        try:
            save_delivery(db, row, delivery)
        except DeliveryLost:
            return None
        return row.chat_id, row.content, delivery

def record_delivery(ident, token, **changes):
    with SessionLocal() as db:
        row = db.scalar(select(ChatMessage).where(ChatMessage.message_id == ident).with_for_update())
        current = dict((row.extra_data or {}).get("channel_delivery") or {}) if row else {}
        if (current.get("token") != token or current.get("status") != "sending"
                or current.get("lease_until", 0) <= time.time()):
            raise DeliveryLost()
        current.update(changes, at=time.time(), lease_until=time.time() + DELIVERY_LEASE_SEC)
        save_delivery(db, row, current)

async def deliver_pending(limit=20):
    """Deliver each text/file once after its receipt, with bounded, leased retries.

    A crash after external acceptance but before storing its receipt can duplicate that
    part: the channel APIs do not provide a shared transaction or idempotency key.
    """
    def pending():
        with SessionLocal() as db:
            state = ChatMessage.extra_data["channel_delivery"]
            return list(db.scalars(select(ChatMessage.message_id).where(
                ChatMessage.chat_id.startswith("pa_chat_"), ChatMessage.role == "assistant",
                or_(state["status"].as_string() == "pending",
                    and_(state["status"].as_string() == "sending",
                         state["lease_until"].as_float() < time.time()))
            ).order_by(ChatMessage.chat_seq).limit(limit)))

    for ident in await asyncio.to_thread(pending):
        claimed = await asyncio.to_thread(claim_delivery, ident)
        if claimed is None: continue
        chat_id, text, delivery = claimed
        origin, token = delivery["origin"], delivery["token"]

        async def record(**changes):
            await asyncio.to_thread(record_delivery, ident, token, **changes)

        try:
            conn = await asyncio.to_thread(connection, origin["channel_id"])
            if not await asyncio.to_thread(delivery_allowed, chat_id, conn):
                await record(status="skipped")
                continue
            if not delivery.get("text_sent"):
                async with asyncio.timeout(SEND_TIMEOUT_SEC):
                    ok = await send_text(conn, unpack(origin["msg"]), text,
                                         delivery.get("text_chunks_sent", 0), record)
                if not ok: raise RuntimeError("Personal channel text delivery failed")
                await record(text_sent=True)
            from integrations.channels.registry import get_adapter
            sent = list(delivery.get("sent_file_ids") or [])
            failed = list(delivery.get("failed_file_ids") or [])
            retry = False
            for file_id in delivery.get("file_ids", []):
                if file_id in sent or file_id in failed: continue
                # Configuration can change while earlier files are being uploaded.
                conn = await asyncio.to_thread(connection, origin["channel_id"])
                if not await asyncio.to_thread(delivery_allowed, chat_id, conn):
                    await record(status="skipped")
                    break
                await record()
                try:
                    async with asyncio.timeout(SEND_TIMEOUT_SEC):
                        content, name, mime = await asyncio.to_thread(load_file, chat_id, file_id)
                        push = getattr(get_adapter(conn.channel_type), "push_file", None)
                        if not callable(push):
                            raise RuntimeError("Personal channel does not support files")
                        result = await push(conn, unpack(origin["msg"]), content, name, mime)
                    if not result.success:
                        log.warning("Personal file delivery rejected message=%s file=%s kind=%s",
                                    ident, file_id, result.error_kind)
                        retry = True
                        continue
                    sent.append(file_id)
                    await record(sent_file_ids=list(sent))
                except OutputUnavailable:
                    failed.append(file_id)
                    await record(failed_file_ids=list(failed))
                    log.warning("Personal file unavailable message=%s file=%s", ident, file_id)
                except DeliveryLost:
                    raise
                except Exception as exc:  # noqa: BLE001 - one failure must not starve other outputs
                    log.warning("Personal file delivery failed message=%s file=%s (%s)",
                                ident, file_id, type(exc).__name__)
                    retry = True
            else:
                status = ("pending" if retry and delivery["attempts"] < MAX_ATTEMPTS
                          else "failed" if retry or failed else "sent")
                await record(status=status)
        except DeliveryLost:
            continue
        except Exception as exc:  # noqa: BLE001 - bounded retry, without leaking channel credentials
            log.warning("Personal channel delivery failed message=%s (%s)", ident, type(exc).__name__)
            try:
                await record(status="pending" if delivery["attempts"] < MAX_ATTEMPTS else "failed")
            except DeliveryLost:
                pass

def notify_soon(origin, text):
    """Schedule a best-effort notice from synchronous code that runs on the event loop thread."""
    if not origin: return
    try:
        asyncio.get_running_loop().create_task(send_now(origin, text))
    except RuntimeError:
        pass

def bots(db, uid):
    """The owner's bots, with the Personal Agent routing flag."""
    from integrations.channels.service import ChannelService, bot_to_dict
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(uid)))
    routed = set((agent.config or {}).get("channel_ids", [])) if agent else set()
    result = []
    for conn in ChannelService(db).list_bots(str(uid), main_only=True):
        item = bot_to_dict(conn)
        if item.get("channel_type") not in SUPPORTED: continue
        result.append({"channel_id": item["channel_id"], "channel_type": item["channel_type"],
            "display_name": item.get("display_name"), "enabled": item.get("enabled"),
            "status": item.get("status"), "personal_agent": item["channel_id"] in routed})
    return result

def route(db, uid, channel_id, enabled):
    from integrations.channels.service import ChannelService
    if channel_id not in {conn.channel_id for conn in ChannelService(db).list_bots(str(uid), main_only=True)}:
        raise LookupError("Bot not found")
    from personal_agent.identity.config import update_config
    def change(config):
        ids = [ident for ident in config.get("channel_ids", []) if ident != channel_id]
        config["channel_ids"] = ids + ([channel_id] if enabled else [])
    update_config(db, uid, change)
    return bots(db, uid)
