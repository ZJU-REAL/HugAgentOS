"""What an IM conversation can do besides talking to the agent.

- The acknowledgement placeholder: sent when a message arrives, then edited into the reply
  (or recalled before it) so no stale "processing" line is left behind.
- Replies to the agent's own messages: quoting a task's result, question or approval notice
  continues that task directly, without asking the agent which task it is about.
- Approvals and questions: a task that needs a decision says so in the IM conversation it
  came from, and a short reply there ("同意" / "拒绝") decides it.
"""
import asyncio
import logging
import re
import time
import uuid

from sqlalchemy import select

from core.db.models import ChatMessage
from personal_agent.models import PersonalAgent, PersonalMessageTask, PersonalWorkItem

log = logging.getLogger(__name__)
PLACEHOLDER = "收到，我看一下…"
# Only explicit words: everyday replies such as "好的" or "ok" must never approve an action.
APPROVE_ONCE = {"同意", "批准", "允许", "approve", "yes"}
APPROVE_TASK = {"全部同意", "同意全部", "都同意", "全部允许", "都允许", "approveall"}
REJECT = {"拒绝", "不同意", "不允许", "reject", "deny", "no"}
# A quoted message counts for the task it reports on, most specific relation first.
RELATIONS = ("question", "approval", "report", "final", "status", "related")
RECALL_ATTEMPTS = 3

def _session():
    """The outbox's own session factory: recalls run in the same loop as its deliveries."""
    from personal_agent.channels import channel_bridge
    return channel_bridge.SessionLocal()
_PUNCT = re.compile(r"[\s。！!.,，~～、]+")

def approval_word(text):
    """once, task or reject when the whole message is an approval answer, otherwise None."""
    word = _PUNCT.sub("", (text or "").strip().lower())
    if word in APPROVE_TASK: return "task"
    if word in APPROVE_ONCE: return "once"
    if word in REJECT: return "reject"
    return None

# ---------------------------------------------------------------- the placeholder

async def send_first(adapter, conn, msg, chunk, placeholder_id, send):
    """Send a reply's first chunk, reusing the placeholder.

    A plain-text reply edits the placeholder in place. A reply in the channel's own markdown
    would lose its format in a text edit, so the placeholder is recalled and the reply sent
    as a new message; a channel that cannot recall gets the edit as a last resort."""
    edit = getattr(adapter, "edit_message", None)
    recall = getattr(adapter, "recall_message", None)
    plain = send == adapter.send_text
    try:
        if edit is not None and (plain or recall is None):
            result = await edit(conn, placeholder_id, chunk)
            if result.success:
                result.message_id = result.message_id or placeholder_id
                return result
        if recall is not None:
            await recall(conn, msg, placeholder_id)
    except Exception:  # noqa: BLE001 - the reply itself must still go out
        log.debug("Personal placeholder reuse failed", exc_info=True)
    return await send(conn, msg, chunk)

def mark_placeholder_recall(db, message_id):
    """A message the agent chose not to answer: recall its placeholder (see recall_pending)."""
    message = db.get(ChatMessage, message_id)
    origin = ((message.extra_data or {}).get("channel_origin") or {}) if message else {}
    if origin.get("placeholder_id"):
        message.extra_data = {**message.extra_data, "channel_placeholder": {"status": "recall", "attempts": 0}}

async def recall_pending(limit=20):
    """Recall the placeholders of unanswered messages, a few attempts each."""
    from integrations.channels.registry import get_adapter
    from personal_agent.channels.channel_bridge import connection, unpack

    def pending():
        with _session() as db:
            state = ChatMessage.extra_data["channel_placeholder"]
            return [(row.message_id, row.extra_data["channel_origin"]) for row in db.scalars(select(ChatMessage).where(
                ChatMessage.chat_id.startswith("pa_chat_"), ChatMessage.role == "user",
                state["status"].as_string() == "recall").limit(limit))]

    def settle(ident, ok):
        with _session() as db:
            row = db.scalar(select(ChatMessage).where(ChatMessage.message_id == ident).with_for_update())
            state = dict((row.extra_data or {}).get("channel_placeholder") or {})
            if state.get("status") != "recall": return
            state["attempts"] = state.get("attempts", 0) + 1
            if ok or state["attempts"] >= RECALL_ATTEMPTS: state["status"] = "done" if ok else "failed"
            row.extra_data = {**row.extra_data, "channel_placeholder": state}
            db.commit()

    for ident, origin in await asyncio.to_thread(pending):
        ok = False
        try:
            conn = await asyncio.to_thread(connection, origin["channel_id"])
            recall = getattr(get_adapter(conn.channel_type), "recall_message", None) if conn else None
            if recall is None: ok = True  # nothing more can be done for this channel
            else: ok = (await recall(conn, unpack(origin["msg"]), origin["placeholder_id"])).success
        except Exception as exc:  # noqa: BLE001
            log.debug("Personal placeholder recall failed (%s)", type(exc).__name__)
        await asyncio.to_thread(settle, ident, ok)

# ---------------------------------------------------------------- replies to the agent's messages

def replied_message(db, agent, chat_ids, platform_id):
    """The agent's message (in this IM conversation or the main one) that an IM reply quotes."""
    if not platform_id: return None
    scope = (ChatMessage.chat_id.in_(chat_ids), ChatMessage.role == "assistant")
    row = db.scalar(select(ChatMessage).where(*scope,
        ChatMessage.extra_data["channel_delivery"]["message_id"].as_string() == platform_id).limit(1))
    if row is not None: return row
    # Later chunks of a long reply: only the first ID is queryable, so check recent rows.
    for row in db.scalars(select(ChatMessage).where(*scope).order_by(ChatMessage.created_at.desc()).limit(300)):
        if platform_id in (((row.extra_data or {}).get("channel_delivery") or {}).get("message_ids") or []): return row
    return None

def linked_task(db, message_id):
    links = db.execute(select(PersonalMessageTask.work_item_id, PersonalMessageTask.relation)
                       .where(PersonalMessageTask.message_id == message_id)).all()
    for relation in RELATIONS:
        for task_id, kind in links:
            if kind == relation: return task_id
    return None

def _notice_matches(task, msg):
    notice = (task.checkpoint or {}).get("approval_notice") or {}
    pending = (task.checkpoint or {}).get("pending_approval") or {}
    # Notices from before the sender was recorded match on the conversation alone.
    sender = notice.get("sender", msg.sender_id)
    return (notice.get("approval_id") == pending.get("id") and notice.get("channel_id") == msg.channel_id
            and notice.get("peer") == msg.external_conversation_id and notice.get("chat_type") == "p2p"
            and sender == msg.sender_id)

def route(db, agent, chat_id, text, msg, from_owner=False):
    """How an IM message is admitted: (intent, task ids). auto lets the agent decide.

    Only a private message from the owner (see channel_bridge.from_owner) can answer an approval."""
    target = replied_message(db, agent, [chat_id, agent.chat_id], getattr(msg, "reply_to_message_id", ""))
    task_id = linked_task(db, target.message_id) if target is not None else None
    word = approval_word(text)
    if word and from_owner and msg.chat_type == "p2p":
        waiting = [task for task in db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id,
                   PersonalWorkItem.status.in_(("waiting", "running"))))
                   if _notice_matches(task, msg)]
        chosen = [task.id for task in waiting if task.id == task_id] or [task.id for task in waiting]
        if chosen:
            intent = {"once": "approve", "task": "approve_task", "reject": "reject"}[word]
            return intent, chosen[:8]
    task = db.get(PersonalWorkItem, task_id) if task_id else None
    # A task the user stopped stays stopped; quoting it is an ordinary message.
    if task is not None and task.status != "cancelled": return "update", [task.id]
    return "auto", []

# ---------------------------------------------------------------- notices for decisions

SESSION_OPERATIONS = {"chat.create": "新建会话", "chat.update": "修改会话", "chat.delete": "删除会话",
                      "chat.send": "向会话发送消息", "run.cancel": "停止运行", "run.steer": "追加指令"}
COMPUTER_ACTIONS = {"click": "点击", "double_click": "双击", "right_click": "右键点击", "move": "移动鼠标",
                    "type": "输入文字", "key": "按键", "scroll": "滚动", "open_browser": "打开网页"}

def describe(action):
    """One line about an action, in the words of the web confirmation bar (approvalText.ts)."""
    kind = action.get("kind")
    where = "云电脑" if action.get("target") == "cloud" else "你的电脑"
    if kind == "email.send":
        to = action.get("to")
        to = ", ".join(to) if isinstance(to, list) else str(to or "")
        return "发送邮件给 " + to + "，主题「" + str(action.get("subject") or "")[:80] + "」"
    if kind == "hugagent.session":
        args = action.get("args") or {}
        subject = str(args.get("title") or args.get("message") or "")[:80]
        return SESSION_OPERATIONS.get(str(action.get("operation")), "会话操作") + ("「" + subject + "」" if subject else "")
    if kind == "computer.action":
        step = action.get("action") or {}
        detail = next((str(step[key]) for key in ("text", "url", "key") if step.get(key) is not None), "")
        return "在" + where + "上" + COMPUTER_ACTIONS.get(str(step.get("type")), "执行操作") + ("：" + detail[:80] if detail else "")
    return str(kind or "一项操作")

def _post(db, task, text, relation):
    """Post a notice for the task where its result would go; returns the delivery origin, if any."""
    from personal_agent.context import AgentContext
    agent = db.get(PersonalAgent, task.agent_id)
    message = AgentContext(db, agent.user_id).conversation.append(agent, "assistant", text, task.id, relation=relation)
    return ((message.extra_data or {}).get("channel_delivery") or {}).get("origin")

def approval_notice(db, task):
    """Tell the IM conversation of a task that an action waits for approval, once per action."""
    from personal_agent.channels.channel_bridge import home_origin, origin_of
    from personal_agent.scheduling.schedules import is_unattended_run
    from personal_agent.tasks.approvals import scope_key
    state = dict(task.checkpoint or {})
    pending = state.get("pending_approval") or {}
    if not pending or (state.get("approval_notice") or {}).get("approval_id") == pending.get("id"): return
    agent = db.get(PersonalAgent, task.agent_id)
    origin = origin_of(db, task_id=task.id) or (home_origin(db, agent.chat_id) if is_unattended_run(task) else None)
    # A web task already shows the confirmation bar; only an IM conversation needs a message.
    if not origin: return
    head = "「" + task.label_text[:40] + "」需要你批准：" + describe(pending.get("action") or {}) + "。"
    if (origin.get("msg") or {}).get("chat_type") == "group":
        text = head + "请在 Personal Agent 网页的「需要你处理」中批准或拒绝。"
    else:
        text = head + "\n回复「同意」允许这一次" + ("，回复「全部同意」允许本任务内的同类操作" if scope_key(pending.get("action") or {}) else "") + "，回复「拒绝」拒绝。"
    sent = _post(db, task, text, "approval") or {}
    msg = sent.get("msg") or {}
    task.checkpoint = {**task.checkpoint, "approval_notice": {"approval_id": pending["id"], "channel_id": sent.get("channel_id"),
        "peer": msg.get("external_conversation_id"), "chat_type": msg.get("chat_type"), "sender": msg.get("sender_id", "")}}

def question_notice(db, task):
    """Ask the task's question in the conversation the task came from, once per question."""
    state = dict(task.checkpoint or {})
    question = state.get("pending_question") or {}
    if not question or question.get("posted"): return
    _post(db, task, "「" + task.label_text[:40] + "」需要你决定：" + question["text"] + "\n直接回复这条消息即可，我会接着处理。", "question")
    task.checkpoint = {**state, "pending_question": {**question, "posted": True}}

def new_question(text):
    return {"id": "q_" + uuid.uuid4().hex[:12], "text": text.strip()[:1000], "asked_at": time.time()}
