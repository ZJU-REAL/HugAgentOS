"""Admit messages quickly. Explicit task controls apply at once; the agent answers the rest.

There is no separate classifier. A message without an explicit task control becomes a
Trigger of the agent's turn (see router/). The agent answers it directly or uses its task
tools to start, change or stop background tasks.
"""
from sqlalchemy import select
from core.db.models import ChatMessage
from personal_agent.models import PersonalMessageInbox
from personal_agent.conversation.router.triggers import OPEN, enqueue, inbox_view  # noqa: F401
from personal_agent.identity.config import locked_agent
from personal_agent.tasks import task_ops

INTENTS = {"auto", "new", "update", "status", "pause", "resume", "cancel", "approve", "approve_task", "reject"}
# Answers to an approval, from an IM conversation (see im_actions.route).
APPROVALS = ("approve", "approve_task", "reject")
def direct(intent, ids):
    """True when the message needs no model: the user already chose the task and the action."""
    return intent in ("new", "pause", "resume", "cancel", *APPROVALS) or (intent == "update" and len(ids) == 1)

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
    # An approval may answer the notice of a task of another conversation: one a proactive check or a
    # schedule started sends its notice to the home bot. The owner is checked either way.
    from personal_agent.context import AgentContext
    owner = AgentContext(svc.db, svc.user_id) if intent in APPROVALS else svc
    for ident in ids: owner.tasks.get(ident)
    if reply_to:
        message = svc.db.get(ChatMessage, reply_to)
        # An IM reply may quote a message of the main conversation: a proactive summary, a scheduled result.
        if not message or message.chat_id not in (chat_id, agent.chat_id): raise LookupError("Reply message not found")
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
    row = enqueue(svc.db, agent, kind="message", message=message, request_key=request_key, payload=payload)
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
        task = task_ops.update_task(svc, message, ids[0], text, attachments=files)
        result = [task.id]
        # An IM reply to a task's message went straight to the task: say where it went.
        if (message.extra_data or {}).get("channel_origin"):
            reply = svc.conversation.append(svc.identity.agent(), "assistant", "已转给" + task.label + "继续处理。", None, reply_to=message.message_id)
            task_ops.link(svc.db, reply.message_id, task.id, "status")
    elif intent in APPROVALS:
        result = list(ids)
        reply = svc.conversation.append(svc.identity.agent(), "assistant", apply_approval(svc, intent, ids), None, reply_to=message.message_id)
        if len(ids) == 1: task_ops.link(svc.db, reply.message_id, ids[0], "status")
    else:
        tasks = [task_ops.control(svc, message, ident, intent) for ident in ids]
        result = [task.id for task in tasks]
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "\n".join(task_ops.status_line(task) for task in tasks), None, reply_to=message.message_id)
        for task in tasks: task_ops.link(svc.db, reply.message_id, task.id, "status")
    row.status, row.decision = "done", {"work_item_ids": list(dict.fromkeys(result)), "direct": True}

def apply_approval(svc, intent, ids):
    """Decide the pending action of one task from an IM answer; the text that answers the user."""
    from personal_agent.context import AgentContext
    from personal_agent.tasks.approvals import approve, scope_for
    if len(ids) != 1:
        return "有 " + str(len(ids)) + " 个操作在等你批准。请引用对应的批准消息回复「同意」或「拒绝」。"
    # Approvals are not limited to this IM conversation's tasks: the notice may come from the home bot.
    main = AgentContext(svc.db, svc.user_id)
    task = main.tasks.get(ids[0], lock=True)
    pending = (task.checkpoint or {}).get("pending_approval")
    if not pending: return task.label + "没有待批准的操作了。"
    notice = (task.checkpoint or {}).get("approval_notice") or {}
    if intent != "reject" and notice.get("web_only") and notice.get("approval_id") == pending.get("id"):
        # The notice could not show the whole action: it is approved where all of it can be read.
        return task.label + "的这项操作内容较长，这里没有显示完整。请在 Personal Agent 网页的「需要你处理」中看过全文后再批准；回复「拒绝」可以直接拒绝。"
    scope = "task" if intent == "approve_task" and scope_for(task, pending.get("action") or {}) else "once"
    try:
        approve(main, task.id, pending["id"], intent != "reject", scope)
    except ValueError:
        return task.label + "的这项操作已经不能在这里批准了，请在网页的「需要你处理」中查看。"
    if intent == "reject": return "已拒绝。" + task.label + "会换一种方式完成，或说明无法完成。"
    return ("已允许" + task.label + "内的同类操作，继续执行。" if scope == "task" else "已批准，" + task.label + "继续执行。")

def pending(svc):
    """Messages the agent has not answered yet, oldest first."""
    agent = svc.identity.agent()
    if not agent: return []
    chat_id = svc.conversation.id()
    return [inbox_view(row) for row in svc.db.scalars(select(PersonalMessageInbox).join(ChatMessage, ChatMessage.message_id == PersonalMessageInbox.message_id).where(
        PersonalMessageInbox.agent_id == agent.id, PersonalMessageInbox.status.in_(OPEN), ChatMessage.chat_id == chat_id)
        .order_by(PersonalMessageInbox.created_at).limit(100))]
