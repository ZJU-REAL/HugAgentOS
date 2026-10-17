"""Admit messages quickly. Explicit task controls apply at once; the agent answers the rest.

There is no separate classifier. A message without an explicit task control becomes one
turn of the agent in the main conversation (see front.py). The agent answers it directly
or uses its task tools to start, change or stop background tasks.
"""
from sqlalchemy import select
from core.db.models import ChatMessage
from personal_agent.models import PersonalMessageInbox
from personal_agent.identity.config import locked_agent
from personal_agent.tasks import task_ops

INTENTS = {"auto", "new", "update", "status", "pause", "resume", "cancel"}
# Statuses of a turn the agent has not finished. "routing" is the pre-agent name of "responding".
OPEN = ("pending", "responding", "routing")

def inbox_view(row):
    from personal_agent.scheduling.heartbeat import is_beat_key
    # A heartbeat check is not an answer the user waits for: the page shows no typing line for it.
    return {"message_id": row.message_id, "status": row.status, "heartbeat": is_beat_key(row.request_key),
        "work_item_ids": (row.decision or {}).get("work_item_ids", [])}

def direct(intent, ids):
    """True when the message needs no model: the user already chose the task and the action."""
    return intent in ("new", "pause", "resume", "cancel") or (intent == "update" and len(ids) == 1)

def admit(svc, content, request_key, artifact_ids=None, work_item_ids=None, intent="auto", reply_to=None, extra=None):
    if not content.strip() or len(content) > 20000: raise ValueError("Message must contain 1 to 20000 characters")
    if intent not in INTENTS: raise ValueError("Unknown message intent")
    ids = list(dict.fromkeys(work_item_ids or []))
    if len(ids) > 8: raise ValueError("Select at most eight tasks")
    if intent not in {"auto", "new"} and not ids: raise ValueError("Select a task first")
    svc.identity.ensure()
    # Serialize admission for one owner, without holding a model call.
    agent = locked_agent(svc.db, svc.user_id)
    chat_id = svc.conversation.id()
    if chat_id != agent.chat_id:
        import hashlib
        request_key = "session:" + hashlib.sha256((chat_id + ":" + request_key).encode()).hexdigest()
    for ident in ids: svc.tasks.get(ident)
    if reply_to:
        message = svc.db.get(ChatMessage, reply_to)
        if not message or message.chat_id != chat_id: raise LookupError("Reply message not found")
    artifacts = svc.tasks.attachments(artifact_ids or [])
    payload = {"content": content, "artifact_ids": artifacts, "work_item_ids": ids, "intent": intent, "reply_to": reply_to}
    if chat_id != agent.chat_id: payload["chat_id"] = chat_id
    old = svc.db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.agent_id == agent.id, PersonalMessageInbox.request_key == request_key))
    if old:
        if old.payload != payload: raise ValueError("Idempotency key conflict")
        svc.db.commit()
        return inbox_view(old)
    if agent.status != "ready" and intent not in {"status", "pause", "cancel"}: raise ValueError("Personal Agent is paused")
    message = svc.conversation.append(agent, "user", content, None, request_key)
    # The origin (for example an IM conversation) commits with the message, before the agent can see it.
    message.extra_data = {**message.extra_data, "artifact_ids": artifacts, **(extra or {})}
    row = PersonalMessageInbox(message_id=message.message_id, agent_id=agent.id, request_key=request_key, payload=payload)
    svc.db.add(row)
    if direct(intent, ids):
        try:
            apply_direct(svc, row, message)
        except Exception:
            svc.db.rollback()
            raise
    svc.db.commit()
    return inbox_view(row)

def apply_direct(svc, row, message):
    """A task card or task conversation chose the task and the action. No model is involved."""
    payload, ids = row.payload, row.payload["work_item_ids"]
    intent, text, files = payload["intent"], payload["content"], payload["artifact_ids"]
    if intent == "new":
        task = task_ops.create(svc, message, text, "message:" + message.message_id + ":0", attachments=files, related=ids)
        result = [task.id, *ids]
    elif intent == "update":
        result = [task_ops.update_task(svc, message, ids[0], text, attachments=files).id]
    else:
        tasks = [task_ops.control(svc, message, ident, intent) for ident in ids]
        result = [task.id for task in tasks]
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "\n".join(task_ops.status_line(task) for task in tasks), None, reply_to=message.message_id)
        for task in tasks: task_ops.link(svc.db, reply.message_id, task.id, "status")
    row.status, row.decision = "done", {"work_item_ids": list(dict.fromkeys(result)), "direct": True}

def pending(svc):
    """Messages the agent has not answered yet, oldest first."""
    agent = svc.identity.agent()
    if not agent: return []
    chat_id = svc.conversation.id()
    return [inbox_view(row) for row in svc.db.scalars(select(PersonalMessageInbox).join(ChatMessage, ChatMessage.message_id == PersonalMessageInbox.message_id).where(
        PersonalMessageInbox.agent_id == agent.id, PersonalMessageInbox.status.in_(OPEN), ChatMessage.chat_id == chat_id)
        .order_by(PersonalMessageInbox.created_at).limit(100))]
