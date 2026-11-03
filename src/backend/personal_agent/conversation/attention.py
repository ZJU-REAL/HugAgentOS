"""What waits for the owner besides tasks, and where an IM message went.

The page lists tasks itself (approvals, questions, failures). This adds the two other things
that can need a person: a message the agent failed to answer, and a reply that could not be
delivered to its IM conversation. Both can be started again from the page.
"""
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import or_, select

from core.db.models import ChatMessage, ChatSession
from core.infra.time import utc_now
from personal_agent.models import PersonalMessageInbox, PersonalMessageTask, PersonalWorkItem
from personal_agent.tasks.views import task_view

# Old failures stop asking for attention; the messages themselves stay in the conversation.
WINDOW = timedelta(days=3)
PREVIEW = 120
SLACK_LINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
MESSAGE_ID = re.compile(r"\b(om_[A-Za-z0-9_-]+|pa_[a-z]+_[A-Za-z0-9_]+)\b")

def agent_chats(db, agent):
    from personal_agent.conversation.conversations import MARKER
    return [agent.chat_id, *db.scalars(select(ChatSession.chat_id).where(ChatSession.user_id == agent.user_id,
        ChatSession.deleted_at.is_(None), ChatSession.extra_data[MARKER].as_string() == agent.id))]

def _scope(svc, agent):
    """The open conversation only; the main conversation sees every IM conversation too."""
    selected = svc.conversation.id()
    return agent_chats(svc.db, agent) if selected == agent.chat_id else [selected]

def _preview(text):
    return " ".join((text or "").split())[:PREVIEW]

def attention(svc):
    agent = svc.identity.agent()
    if agent is None: return {"turns": [], "deliveries": []}
    chats, since = _scope(svc, agent), utc_now() - WINDOW
    turns = svc.db.execute(select(PersonalMessageInbox, ChatMessage).join(ChatMessage,
        ChatMessage.message_id == PersonalMessageInbox.message_id).where(PersonalMessageInbox.agent_id == agent.id,
        PersonalMessageInbox.status == "failed", ChatMessage.chat_id.in_(chats), ChatMessage.created_at >= since)
        .order_by(ChatMessage.created_at.desc()).limit(20)).all()
    state = ChatMessage.extra_data["channel_delivery"]
    deliveries = svc.db.scalars(select(ChatMessage).where(ChatMessage.chat_id.in_(chats), ChatMessage.role == "assistant",
        state["status"].as_string() == "failed", ChatMessage.created_at >= since)
        .order_by(ChatMessage.created_at.desc()).limit(20)).all()
    return {
        "turns": [{"message_id": message.message_id, "chat_id": message.chat_id, "preview": _preview(message.content),
                   "kind": (inbox.payload or {}).get("kind") or "message", "at": message.created_at.isoformat()}
                  for inbox, message in turns],
        "deliveries": [{"message_id": row.message_id, "chat_id": row.chat_id, "preview": _preview(row.content),
                        "channel_type": ((row.extra_data["channel_delivery"].get("origin") or {}).get("channel_type")),
                        "at": row.created_at.isoformat()} for row in deliveries],
    }

def reprocess(svc, message_id):
    """Answer a failed message again: it goes back to the inbox with the rest of its failed batch."""
    from personal_agent.conversation.router.triggers import inbox_view, reprocess as again
    agent = svc.identity.agent()
    row = svc.db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.message_id == message_id,
        PersonalMessageInbox.agent_id == (agent.id if agent else None)).with_for_update())
    if row is None: raise LookupError("Message not found")
    if row.status != "failed": raise ValueError("Only a message that failed can be processed again")
    again(svc.db, row)
    svc.db.commit()
    return inbox_view(row)

def retry_delivery(svc, message_id):
    from personal_agent.channels.channel_bridge import retry_delivery as retry
    agent = svc.identity.agent()
    if agent is None: raise LookupError("Delivery not found")
    return retry(svc.db, agent, message_id)

def candidates(query):
    """Message IDs a pasted link or ID can stand for, as the channels store them."""
    found = {query}
    for channel, seconds, micros in SLACK_LINK.findall(query):
        found.add(channel + ":" + seconds + "." + micros)
    found.update(MESSAGE_ID.findall(query))
    if query.startswith(("http://", "https://")):
        found.update(value for _, value in parse_qsl(urlparse(query).query) if len(value) >= 8)
    return [item for item in found if item][:20]

def search(svc, query):
    """Messages and tasks that a pasted IM link, a message ID or a piece of text points at."""
    query = (query or "").strip()[:500]
    agent = svc.identity.agent()
    if agent is None or not query: return {"messages": [], "tasks": []}
    chats, ids = agent_chats(svc.db, agent), candidates(query)
    origin_id = ChatMessage.extra_data["channel_origin"]["msg"]["message_id"].as_string()
    sent_id = ChatMessage.extra_data["channel_delivery"]["message_id"].as_string()
    rows = svc.db.scalars(select(ChatMessage).where(ChatMessage.chat_id.in_(chats),
        ChatMessage.role.in_(["user", "assistant"]),
        or_(ChatMessage.message_id.in_(ids), origin_id.in_(ids), sent_id.in_(ids)))
        .order_by(ChatMessage.created_at.desc()).limit(20)).all()
    if not rows:
        # Later chunks of a long reply: the same lookup as a quoted IM reply.
        from personal_agent.channels.im_actions import replied_message
        rows = list(dict.fromkeys(row for row in (replied_message(svc.db, agent, chats, ident) for ident in ids) if row is not None))
    if not rows and len(query) >= 2:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = svc.db.scalars(select(ChatMessage).where(ChatMessage.chat_id.in_(chats),
            ChatMessage.role.in_(["user", "assistant"]), ChatMessage.content.ilike(pattern, escape="\\"))
            .order_by(ChatMessage.created_at.desc()).limit(20)).all()
    links = {}
    for message_id, task_id in svc.db.execute(select(PersonalMessageTask.message_id, PersonalMessageTask.work_item_id)
                                              .where(PersonalMessageTask.message_id.in_([row.message_id for row in rows]))):
        links.setdefault(message_id, []).append(task_id)
    for inbox in svc.db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.message_id.in_([row.message_id for row in rows]))):
        links.setdefault(inbox.message_id, []).extend((inbox.decision or {}).get("work_item_ids") or [])
    task_ids = list(dict.fromkeys([*(item for row in rows for item in links.get(row.message_id, [])),
                                   *(item for item in ids if item.startswith("pa_task_"))]))
    tasks = svc.db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id,
                                                          PersonalWorkItem.id.in_(task_ids))).all()
    return {
        "messages": [{"message_id": row.message_id, "chat_id": row.chat_id, "role": row.role, "seq": row.chat_seq,
                      "preview": _preview(row.content), "at": row.created_at.isoformat(),
                      "channel_type": ((row.extra_data or {}).get("channel_origin") or {}).get("channel_type"),
                      "task_ids": list(dict.fromkeys(links.get(row.message_id, [])))} for row in rows],
        "tasks": [task_view(task) for task in tasks],
    }
