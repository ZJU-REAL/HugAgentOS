"""Read-only sidebar projection; no agent initialization or model calls."""
from sqlalchemy import func, select
from core.db.models import ChatMessage
from personal_agent.models import PersonalMessageTask, PersonalWorkItem


def _final_assistant(agent):
    return (
        ChatMessage.chat_id == agent.chat_id,
        ChatMessage.role == "assistant",
        func.coalesce(ChatMessage.extra_data["streaming"].as_boolean(), False).is_(False),
    )


def _read_seq(svc, agent) -> int:
    from core.chat.read_state import read_seqs

    return read_seqs(svc.db, svc.user_id, [agent.chat_id]).get(agent.chat_id, 0)


def sidebar_status(svc, after_seq: int):
    agent = svc.identity.agent()
    if agent is None:
        return {"tasks": [], "replying": False, "latest_seq": 0, "unread_count": 0, "read_seq": 0}
    final_assistant = _final_assistant(agent)
    # The server read position is shared by every client; a client's own newer
    # position (``after_seq``) still counts while its mark-read request is in flight.
    read_seq = _read_seq(svc, agent)
    unread = svc.db.scalar(select(func.count()).select_from(ChatMessage).where(
        *final_assistant, ChatMessage.chat_seq > max(after_seq, read_seq))) or 0
    latest = svc.db.scalar(select(func.max(ChatMessage.chat_seq)).where(*final_assistant)) or 0
    from personal_agent.conversation.message_routing import pending
    turns = pending(svc)
    tasks = [{key: task.get(key) for key in ("id", "status", "run_id", "updated_at",
        "pending_approval", "uncertain_actions")} for task in svc.tasks.list()]
    # Only a delivered result can acknowledge a completed work item. Earlier
    # progress/status messages must not silently clear a later completion.
    reports = svc.db.execute(select(PersonalMessageTask.work_item_id,
        func.max(ChatMessage.chat_seq)).join(ChatMessage,
        ChatMessage.message_id == PersonalMessageTask.message_id).join(PersonalWorkItem,
        PersonalWorkItem.id == PersonalMessageTask.work_item_id).where(
        *final_assistant, ChatMessage.created_at >= PersonalWorkItem.updated_at,
    ).group_by(PersonalMessageTask.work_item_id)).all()
    result_seq = dict(reports)
    for task in tasks:
        task["result_seq"] = result_seq.get(task["id"])
    return {"tasks": tasks, "replying": any(not turn.get("heartbeat") for turn in turns),
            "latest_seq": latest, "unread_count": unread, "read_seq": read_seq}


def mark_read(svc, seq: int):
    """Move the conversation's server read position forward to ``seq`` (never back).

    The position cannot pass the latest final reply: reading cannot pre-acknowledge
    messages that have not been delivered yet.
    """
    from core.chat.read_state import advance

    agent = svc.identity.agent()
    if agent is None:
        raise LookupError("Personal Agent is not initialized")
    latest = svc.db.scalar(select(func.max(ChatMessage.chat_seq)).where(*_final_assistant(agent))) or 0
    result = advance(svc.db, svc.user_id, agent.chat_id, min(int(seq), int(latest)))
    svc.db.commit()
    return {"read_seq": result["read_seq"], "latest_seq": latest}
