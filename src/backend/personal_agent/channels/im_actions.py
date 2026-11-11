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

def quoted(db, agent, chat_id, msg):
    """The id of the agent's message that this IM message quotes, or None."""
    target = replied_message(db, agent, [chat_id, agent.chat_id], getattr(msg, "reply_to_message_id", ""))
    return target.message_id if target is not None else None

def route(db, agent, chat_id, text, msg, from_owner=False):
    """How an IM message is admitted: (intent, task ids). auto lets the agent decide.

    Only a private message from the owner (see channel_bridge.from_owner) can answer an approval."""
    target = replied_message(db, agent, [chat_id, agent.chat_id], getattr(msg, "reply_to_message_id", ""))
    task_id = linked_task(db, target.message_id) if target is not None else None
    word = approval_word(text)
    from personal_agent.scheduling import proactive_tasks
    from personal_agent.scheduling.schedules import is_proactive
    if word and from_owner and msg.chat_type == "p2p":
        quoted = target.message_id if target is not None else None
        # What a proactive check asks for is the agent's own idea. A bare "同意" may be the answer to
        # something else the user was asked, so only a reply that quotes that very notice decides it.
        waiting = [task for task in db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id,
                   PersonalWorkItem.status.in_(("waiting", "running"))))
                   if _notice_matches(task, msg) and (not proactive_tasks.began(task) or (quoted is not None
                       and (task.checkpoint or {}).get("approval_notice", {}).get("message_id") == quoted))]
        chosen = [task.id for task in waiting if task.id == task_id] or [task.id for task in waiting]
        if chosen:
            intent = {"once": "approve", "task": "approve_task", "reject": "reject"}[word]
            return intent, chosen[:8]
    task = db.get(PersonalWorkItem, task_id) if task_id else None
    # A task the user stopped stays stopped; quoting it is an ordinary message. So is quoting what a
    # proactive check wrote: "先别发" must not hand the task its full tools, so the agent reads the
    # reply and continues the task only when the user asks for that.
    if task is not None and task.status != "cancelled" and not is_proactive(task): return "update", [task.id]
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

# The longest text of an action a notice shows. A longer one is approved in the web application, where all of it is shown.
NOTICE_BODY_CHARS = 800
# First line of the request for a mail to an address that was refused as the recipient of a reply in the same task.
NOT_SENDER = "【注意】这封信的收件人不是原邮件的发件人。我先按回复提交时已被拦下，请确认你确实要发给这个地址。"
UNASKED = "这是我在主动服务中自己提出的操作，不是你要求的。"
# For a task the user asked for in answer to a proactive summary: they asked, but what is sent was prepared unwatched.
PREPARED = "这是你看过主动服务的汇总后让我做的，内容是我当时自己准备的，请核对后再同意。"

def _place(db, agent, action):
    """(where, chat title) of a session action: the cloud or one of the user's computers, and the chat it changes."""
    from core.db.models import ChatRun, ChatSession
    from personal_agent.models import PersonalDevice
    target, args = str(action.get("target") or "cloud"), action.get("args") or {}
    if target == "cloud":
        where = "云端的 HugAgentOS"
        chat_id = args.get("chat_id")
        if not chat_id and args.get("run_id"):
            run = db.get(ChatRun, args["run_id"])
            chat_id = run.chat_id if run is not None and run.user_id == agent.user_id else None
        chat = db.get(ChatSession, chat_id) if chat_id else None
        title = chat.title if chat is not None and chat.user_id == agent.user_id else None
        return where, title or (("编号 " + str(chat_id)) if chat_id else None)
    device = db.get(PersonalDevice, target.split(":", 1)[-1])
    name = device.name if device is not None and device.user_id == agent.user_id else target
    return "你的电脑「" + str(name)[:60] + "」上的 HugAgentOS", ("编号 " + str(args["chat_id"])) if args.get("chat_id") else None

def explain(db, agent, action):
    """(lines, body) that say everything about an action of a task a proactive check started:
    what, where, in which chat, and the whole text that would go out."""
    kind = action.get("kind")
    if kind == "email.send":
        to = action.get("to")
        names = [str(item.get("path") or "").rsplit("/", 1)[-1] for item in action.get("attachments") or []]
        lines = ["操作：发送邮件", "发件邮箱：" + str(action.get("from") or ""),
                 "收件人：" + (", ".join(to) if isinstance(to, list) else str(to or "")), "主题：" + str(action.get("subject") or "")]
        if action.get("not_original_sender"): lines.insert(0, NOT_SENDER)
        return lines + (["附件：" + "、".join(names)] if names else []), str(action.get("body") or "")
    if kind == "hugagent.session":
        args = action.get("args") or {}
        where, chat = _place(db, agent, action)
        lines = ["操作：" + SESSION_OPERATIONS.get(str(action.get("operation")), "会话操作（" + str(action.get("operation")) + "）"), "位置：" + where]
        if chat: lines.append("对话：「" + chat[:120] + "」")
        if args.get("title"): lines.append("新标题：「" + str(args["title"]) + "」")
        return lines, str(args.get("message") or "")
    return ["操作：" + describe(action)], ""

def proactive_notice(db, agent, task, action, group):
    """(text, web_only) of the approval notice for a task a proactive check started. The user did not
    ask for the action and may read the notice on a phone: it names the place and the chat and shows
    the whole text, or it sends the user to the web application and takes no "同意" here."""
    lines, body = explain(db, agent, action)
    web_only = group or len(body) > NOTICE_BODY_CHARS
    from personal_agent.scheduling.proactive_tasks import started_by_check
    head = ["「" + _label(task) + "」需要你批准一项操作。" + (UNASKED if started_by_check(task) else PREPARED), *lines]
    if body and len(body) > NOTICE_BODY_CHARS:
        head.append("内容（共 " + str(len(body)) + " 字，这里只显示开头）：\n" + body[:200] + "……")
    elif body:
        head.append("内容（全文）：\n" + body)
    if group: tail = "请在 Personal Agent 网页的「需要你处理」中批准或拒绝。"
    elif web_only: tail = "内容较长，这里没有显示完整。请在 Personal Agent 网页的「需要你处理」中看过全文后再批准；引用这条消息回复「拒绝」可以直接拒绝。"
    else: tail = "引用这条消息回复「同意」允许这一次，回复「拒绝」拒绝。不回复则不会执行。"
    return "\n".join([*head, tail]), web_only

def _label(task):
    """The task's title as a notice shows it: without internal marks."""
    from personal_agent.scheduling.proactive_tasks import plain
    return plain(task.label_text)[:40]

def _post(db, task, text, relation):
    """Post a notice for the task where its result would go; returns the message."""
    from personal_agent.context import AgentContext
    agent = db.get(PersonalAgent, task.agent_id)
    return AgentContext(db, agent.user_id).conversation.append(agent, "assistant", text, task.id, relation=relation)

def approval_notice(db, task):
    """Tell the IM conversation of a task that an action waits for approval, once per action."""
    from personal_agent.channels.channel_bridge import home_origin, origin_of
    from personal_agent.scheduling.schedules import is_unattended_run
    from personal_agent.tasks.approvals import scope_key
    state = dict(task.checkpoint or {})
    pending = state.get("pending_approval") or {}
    if not pending or (state.get("approval_notice") or {}).get("approval_id") == pending.get("id"): return
    agent = db.get(PersonalAgent, task.agent_id)
    from personal_agent.scheduling import proactive_tasks
    # A task a proactive check started has no IM message of its own, also after the user took it over:
    # each of its actions asks, so the request must reach the user where the summary did.
    # A follow-up the user asked for has the conversation they asked in.
    home = is_unattended_run(task) or proactive_tasks.started_by_check(task)
    origin = origin_of(db, task_id=task.id) or (home_origin(db, agent.chat_id) if home else None)
    # A web task already shows the confirmation bar; only an IM conversation needs a message.
    if not origin: return
    group, web_only = (origin.get("msg") or {}).get("chat_type") == "group", False
    head = "「" + _label(task) + "」需要你批准：" + describe(pending.get("action") or {}) + "。"
    if proactive_tasks.began(task):
        text, web_only = proactive_notice(db, agent, task, pending.get("action") or {}, group)
    elif group:
        text = head + "请在 Personal Agent 网页的「需要你处理」中批准或拒绝。"
    else:
        text = head + "\n回复「同意」允许这一次" + ("，回复「全部同意」允许本任务内的同类操作" if scope_key(pending.get("action") or {}) else "") + "，回复「拒绝」拒绝。"
    message = _post(db, task, text, "approval")
    if home and not (message.extra_data or {}).get("channel_delivery"):
        from personal_agent.channels.channel_bridge import mark_for_delivery
        mark_for_delivery(db, message, task.id, home=True)
    sent = ((message.extra_data or {}).get("channel_delivery") or {}).get("origin") or {}
    msg = sent.get("msg") or {}
    task.checkpoint = {**task.checkpoint, "approval_notice": {"approval_id": pending["id"], "channel_id": sent.get("channel_id"),
        "peer": msg.get("external_conversation_id"), "chat_type": msg.get("chat_type"), "sender": msg.get("sender_id", ""),
        "message_id": message.message_id, **({"web_only": True} if web_only else {})}}

def question_notice(db, task):
    """Ask the task's question in the conversation the task came from, once per question."""
    state = dict(task.checkpoint or {})
    question = state.get("pending_question") or {}
    if not question or question.get("posted"): return
    from personal_agent.scheduling.proactive_tasks import plain
    _post(db, task, "「" + _label(task) + "」需要你决定：" + plain(question["text"]) + "\n直接回复这条消息即可，我会接着处理。", "question")
    task.checkpoint = {**state, "pending_question": {**question, "posted": True}}

def new_question(text):
    return {"id": "q_" + uuid.uuid4().hex[:12], "text": text.strip()[:1000], "asked_at": time.time()}
