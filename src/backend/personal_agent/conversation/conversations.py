"""Conversation routing for one identity: web main plus isolated IM peers/rooms."""
import hashlib
from sqlalchemy import select
from core.db.models import ChatSession, ChatMessage
from integrations.channels.models import ChannelConnection
from personal_agent.models import PersonalMessageTask

MARKER = "personal_agent_channel"

def require_session(db, agent, chat_id=None):
    if agent is None: raise LookupError("Conversation not found")
    ident = chat_id or agent.chat_id
    row = db.get(ChatSession, ident)
    if (not row or row.user_id != agent.user_id or row.deleted_at
            or (ident != agent.chat_id and (row.extra_data or {}).get(MARKER) != agent.id)):
        raise LookupError("Conversation not found")
    return row

def channel_session(db, agent, conn, msg):
    """Caller holds the agent admission lock. IDs also fence duplicate creates."""
    if str(conn.owner_user_id) != agent.user_id:
        raise LookupError("Channel not found")
    peer = str(msg.external_conversation_id or "").strip()
    if not peer or len(peer) > 128: raise ValueError("Channel conversation ID unavailable")
    kind = "group" if msg.chat_type == "group" else "p2p"
    key = hashlib.sha256((agent.id + "\0" + conn.channel_id + "\0" + kind + "\0" + peer).encode()).hexdigest()[:40]
    ident = "pa_chat_channel_" + key
    row = db.get(ChatSession, ident)
    if row is None:
        name = (getattr(msg, "sender_name", "") or peer) if kind == "p2p" else peer
        row = ChatSession(chat_id=ident, user_id=agent.user_id, title=name[:200],
            channel_id=conn.channel_id, external_conversation_id=peer,
            extra_data={MARKER: agent.id, "personal_agent": True, "channel_type": conn.channel_type,
                        "channel_chat_type": kind})
        db.add(row); db.flush()
    from personal_agent.channels.channel_bridge import pack
    row.extra_data = {**(row.extra_data or {}), "channel_origin": {
        "channel_id": conn.channel_id, "channel_type": conn.channel_type, "msg": pack(msg)}}
    return require_session(db, agent, ident)

def task_chat(db, agent, task):
    """The source conversation survives task execution in its private work session."""
    ident = (task.checkpoint or {}).get("source_chat_id")
    if not ident:
        ident = db.scalar(select(ChatMessage.chat_id).join(PersonalMessageTask,
            PersonalMessageTask.message_id == ChatMessage.message_id).where(
            PersonalMessageTask.work_item_id == task.id, PersonalMessageTask.relation == "goal")
            .order_by(ChatMessage.created_at, ChatMessage.message_id).limit(1))
    return require_session(db, agent, ident).chat_id

def destination(db, agent, *, selected=None, task=None, reply_to=None):
    if reply_to:
        message = db.get(ChatMessage, reply_to)
        if message is None: raise LookupError("Reply message not found")
        return require_session(db, agent, message.chat_id).chat_id
    if task is not None: return task_chat(db, agent, task)
    return require_session(db, agent, selected).chat_id

def latest_origin(db, agent, chat_id):
    session = require_session(db, agent, chat_id)
    if session.chat_id == agent.chat_id: return None
    conn = db.get(ChannelConnection, session.channel_id)
    if (not conn or conn.owner_user_id != agent.user_id or not conn.enabled
            or conn.channel_id not in (agent.config or {}).get("channel_ids", [])):
        raise ValueError("Channel is no longer connected to Hugo")
    if origin := (session.extra_data or {}).get("channel_origin"): return origin
    origins = db.scalars(select(ChatMessage.extra_data).where(
        ChatMessage.chat_id == chat_id, ChatMessage.role == "user")
        .order_by(ChatMessage.chat_seq.desc()).limit(100))
    for metadata in origins:
        origin = (metadata or {}).get("channel_origin")
        if origin: return origin
    raise ValueError("Send a message from this channel first")

def listing(svc):
    agent = svc.identity.agent()
    if agent is None: return []
    # Materialize known legacy routes without moving or rewriting their messages.
    # Historical mixed transcripts are retained; each route starts an isolated history.
    from personal_agent.channels.channel_bridge import unpack
    from personal_agent.identity.config import locked_agent
    agent = locked_agent(svc.db, svc.user_id)
    if not (agent.config or {}).get("channel_sessions_seeded"):
        known = set()
        rows = svc.db.scalars(select(ChatMessage.extra_data).where(
            ChatMessage.chat_id == agent.chat_id, ChatMessage.role == "user",
            ChatMessage.extra_data["channel_origin"].isnot(None))
            .order_by(ChatMessage.chat_seq.desc()).limit(2000))
        for metadata in rows:
            origin = (metadata or {}).get("channel_origin") or {}
            msg = origin.get("msg") or {}
            key = (origin.get("channel_id"), msg.get("chat_type"), msg.get("external_conversation_id"))
            if key in known or not key[0] or not key[2]: continue
            known.add(key)
            conn = svc.db.get(ChannelConnection, key[0])
            if conn and conn.owner_user_id == svc.user_id and conn.channel_id in (agent.config or {}).get("channel_ids", []):
                channel_session(svc.db, agent, conn, unpack(msg))
        agent.config = {**(agent.config or {}), "channel_sessions_seeded": True}
    svc.db.commit()
    sessions = svc.db.scalars(select(ChatSession).where(
        ChatSession.user_id == svc.user_id, ChatSession.deleted_at.is_(None),
        ChatSession.extra_data[MARKER].as_string() == agent.id)
        .order_by(ChatSession.last_message_at.desc().nullslast(), ChatSession.created_at.desc())).all()
    result = []
    for session in sessions:
        last = svc.db.scalar(select(ChatMessage).where(ChatMessage.chat_id == session.chat_id,
            ChatMessage.role.in_(["user", "assistant"])).order_by(ChatMessage.chat_seq.desc()).limit(1))
        conn = svc.db.get(ChannelConnection, session.channel_id)
        result.append({"chat_id": session.chat_id, "title": session.title,
            "channel_id": session.channel_id, "channel_type": (session.extra_data or {}).get("channel_type"),
            "chat_type": (session.extra_data or {}).get("channel_chat_type"),
            "bot_name": conn.display_name if conn else "",
            "preview": last.content[:100] if last else "",
            "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None})
    return result
