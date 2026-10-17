"""Task-scoped instructions and bulk message associations."""
from sqlalchemy import select, and_, or_, exists
from core.db.models import ChatMessage
from personal_agent.models import PersonalMessageTask, PersonalWorkItem

def instructions(db, chat_id, task_id, limit=30):
    # SQLite upgrades use metadata reconciliation, so legacy JSON links need a read fallback.
    linked = exists(select(PersonalMessageTask.message_id).where(PersonalMessageTask.message_id == ChatMessage.message_id)).correlate(ChatMessage)
    task = db.get(PersonalWorkItem, task_id)
    legacy = and_(~linked, ChatMessage.extra_data["work_item_id"].as_string() == task_id,
        ChatMessage.role == "user", ChatMessage.chat_seq != task.context_seq)
    rows = db.execute(select(ChatMessage, PersonalMessageTask).outerjoin(PersonalMessageTask,
        and_(PersonalMessageTask.message_id == ChatMessage.message_id, PersonalMessageTask.work_item_id == task_id))
        .where(or_(PersonalMessageTask.relation == "update", and_(ChatMessage.chat_id == chat_id, legacy)))
        .order_by(ChatMessage.chat_seq.desc()).limit(limit)).all()
    return [link.instruction if link else message.content for message, link in reversed(rows)]

def message_links(db, message_ids):
    result = {ident: [] for ident in message_ids}
    if not result: return result
    for link in db.scalars(select(PersonalMessageTask).where(PersonalMessageTask.message_id.in_(message_ids))):
        result[link.message_id].append({"work_item_id": link.work_item_id, "relation": link.relation})
    missing = [ident for ident, links in result.items() if not links]
    for message in db.scalars(select(ChatMessage).where(ChatMessage.message_id.in_(missing))):
        task_id = (message.extra_data or {}).get("work_item_id")
        task = db.get(PersonalWorkItem, task_id) if task_id else None
        if task:
            from personal_agent.models import PersonalAgent
            if db.get(PersonalAgent, task.agent_id).chat_id == message.chat_id:
                result[message.message_id].append({"work_item_id": task.id, "relation": "legacy"})
    return result
