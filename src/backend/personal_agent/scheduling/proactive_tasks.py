"""Limits of a proactive task: the task a heartbeat turn starts, while the user has not taken it over.

Which task is one is decided by ``schedules.is_proactive``. This module holds what the limits
share (PROACTIVE_DESIGN §6): the drafts folder, and how such a task ends when it
fails. Nobody asked for the task, so its failure is counted and nobody is messaged.
"""
import logging
import re
import time

from sqlalchemy import select, update

from personal_agent.models import PersonalAgent, PersonalDeviceCommand, PersonalWorkItem
from personal_agent.workspace.paths import workspace_path

log = logging.getLogger(__name__)
# The only folder the file tools of a proactive task may write to.
DRAFTS = "/workspace/drafts/"
LIVE = ("queued", "running", "waiting", "computer_wait")
FAILED = "主动服务未完成"
INTERRUPTED = "主动服务被中断"
APPROVAL_EXPIRED = "批准请求超过 24 小时没有答复，已作废"
REVOKED = "智能体已暂停或权限已收回，主动服务已停止"
# An approval a proactive task asked for is void after this long: an action nobody asked for must not stay
# one "同意" away for days.
PENDING_MAX_SEC = 86400

# Set on a task the user asked for in answer to a proactive summary ("按草稿发吧").
FOLLOWUP = "proactive_followup"

def started_by_check(task):
    """A task a heartbeat turn started, whoever continues it now."""
    from personal_agent.scheduling.heartbeat import is_beat_key
    return task is not None and is_beat_key(task.request_key or "")

def began(task):
    """A task that goes back to a proactive check: one a heartbeat turn started (whoever continues it now),
    or one the user asked for in answer to a proactive summary (mark_followup). Its actions need approval
    one by one for good: what it acts on (a draft, a recipient) was prepared from mail, pages and chats
    while nobody watched.

    Only the approval rules follow from this. The limits of an unattended task (no questions,
    quiet failure, late in the queue) follow from ``schedules.is_proactive``, which a follow-up never is."""
    return task is not None and (started_by_check(task) or bool((task.checkpoint or {}).get(FOLLOWUP)))

def mark_followup(task):
    """Mark a new task as asked for in answer to a proactive summary. The caller commits.

    The Router calls it in pa_task_create when the user's message quotes or answers such a summary,
    or the goal uses what a proactive task prepared (a draft under DRAFTS)."""
    task.checkpoint = {**(task.checkpoint or {}), FOLLOWUP: True}

def seen_mail(task):
    """The uids of the mail this task's email tools returned."""
    return set((task.checkpoint or {}).get("mail_seen") or [])

MAIL_SEEN_MAX = 2000

def saw_mail(task, uids):
    """Remember mail the task's email tools returned (pa_report checks its mail items against it). The caller commits."""
    known = list((task.checkpoint or {}).get("mail_seen") or [])
    new = [uid for uid in dict.fromkeys(str(uid) for uid in uids) if uid not in known]
    if new: task.checkpoint = {**(task.checkpoint or {}), "mail_seen": (known + new)[-MAIL_SEEN_MAX:]}

MAIL_MARK = re.compile(r"[(（][ \t]*(?:ref[ \t]*[:：]?[ \t]*)?email[:_][ \t]*(\d+)[ \t]*[)）]", re.I)

_REF = (r"(?:(?:task|approval|run|chat|email|msg)[:：_](?![\w.-]*@)(?=[\w-]*\d)[\w:./-]{1,160}"
        r"|pa_[a-z]{1,12}_(?=[\w-]*\d)[\w-]{1,160})")
# A mark in brackets, as the summaries of a proactive check carry them: (email:5002), （ref: drafts）.
REF_MARK = re.compile(r"[ \t]*[(（][ \t]*(?:ref\b[^()（）\n]{0,300}|" + _REF + r")[ \t]*[)）]", re.I)
# The same ids anywhere in a text, bracketed or not.
REF_INSIDE = re.compile(r"[(（][ \t]*ref[ \t]*[:：]|(?<![\w@./-])" + _REF, re.I)

def plain(text):
    """The text without bracketed internal marks, for what the user reads outside a summary: a question,
    a notice, a reason. Other brackets are the writer's own words and stay."""
    clean = REF_MARK.sub("", text or "")
    if clean == (text or ""): return text or ""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in clean.split("\n"))).strip()

def inner_refs(action):
    """The internal references in what an action would send to someone else: the subject and body of
    a mail, the message and title of a session action. They mean nothing to whoever receives them."""
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    texts = [action.get("subject"), action.get("body"), args.get("message"), args.get("title")]
    return list(dict.fromkeys(match.group(0).strip() for text in texts if isinstance(text, str)
                              for match in REF_INSIDE.finditer(text)))[:5]

def refused_recipient(task, recipient):
    """True when a reply of this task to this address was refused: it is not whom the original mail came from."""
    return (recipient or "").strip().lower() in ((task.checkpoint or {}).get("reply_refused") or [])

def refuse_recipient(task, recipient):
    """Remember the refusal: the same address in a later mail of this task is pointed out to the user. The caller commits."""
    known = list((task.checkpoint or {}).get("reply_refused") or [])
    address = (recipient or "").strip().lower()
    if address and address not in known: task.checkpoint = {**(task.checkpoint or {}), "reply_refused": (known + [address])[-50:]}

def known_mail_only(task, summary):
    """(summary without the paragraphs about mail this task never saw, the uids dropped).

    A paragraph marked (email:<uid>) is kept only when a mail tool of this task returned that uid:
    a model has reported mail that did not exist, and later summaries repeated it as a fact."""
    seen, kept, dropped = seen_mail(task), [], []
    for part in re.split(r"\n[ \t]*\n", summary or ""):
        unknown = [uid for uid in MAIL_MARK.findall(part) if uid not in seen]
        if unknown: dropped += unknown
        else: kept.append(part)
    return "\n\n".join(kept).strip(), dropped

def draft_path(path):
    """The normalized path, when it is inside the drafts folder."""
    target = workspace_path(path)
    if not target.startswith(DRAFTS):
        raise PermissionError("A proactive check can write only under " + DRAFTS + ". Save the draft there and report its path.")
    return target

def settled(agent_id, outcome):
    """Count how a proactive task ended. Call it after the commit: record() locks the agent row in its own session."""
    try:
        from personal_agent.scheduling import heartbeat
        heartbeat.record(agent_id, outcome)
    except Exception as exc:
        log.warning("Proactive outcome was not recorded (%s)", type(exc).__name__)

def fails_quietly(task, asg, fallback=""):
    """True when this Assignment ends a proactive task without a result and nothing waits for the user."""
    from personal_agent.scheduling.schedules import is_proactive
    from personal_agent.tasks.worker import blocked
    from personal_agent.worker.settle import report_of
    if not is_proactive(task) or blocked(task) or (task.checkpoint or {}).get("decided_during_run"):
        return False
    status = report_of(task, asg).get("status")
    return status == "failed" or (status not in ("ready", "dropped", "waiting") and not (fallback or "").strip())

def gives_up(task, resumes=True):
    """True when an interrupted proactive task would only be paused with a notice to the user:
    nothing waits for them, and it will not restart by itself (``resumes`` False: it must not)."""
    from personal_agent.scheduling.schedules import is_proactive
    from personal_agent.tasks.worker import blocked, resumable
    if not is_proactive(task) or blocked(task) or (task.checkpoint or {}).get("approved_action_payloads"):
        return False
    return not (resumes and resumable(task))

def fail_quietly(db, asg, task, reason=""):
    """End the Assignment and the task as failed, without the notice an ordinary task gets."""
    from personal_agent.context import AgentContext
    from personal_agent.worker import settle
    settle.end(asg, "failed")
    # Commands it left running no longer wake a Worker for a task that is over.
    settle.detach(db, asg.chat_id)
    uid = db.get(PersonalAgent, task.agent_id).user_id
    AgentContext(db, uid).tasks.drop_failed(task, reason or settle.report_of(task, asg).get("summary") or "")

def expire(db, now=None):
    """Reject the approvals a proactive task asked for that nobody answered for PENDING_MAX_SEC, and end
    those tasks; the number ended. A proactive task has no time limit of its own: it runs as long as
    any other task."""
    from personal_agent.scheduling.heartbeat import KEY_PREFIX
    from personal_agent.scheduling.schedules import is_proactive
    now = now or time.time()
    rows = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.request_key.like(KEY_PREFIX + "%"),
        PersonalWorkItem.status == "waiting")
        .with_for_update(skip_locked=True)).all()
    cancelled = []
    for task in rows:
        pending = (task.checkpoint or {}).get("pending_approval")
        if not pending or not is_proactive(task): continue
        if now - (pending.get("at") or task.updated_at.timestamp()) < PENDING_MAX_SEC: continue
        _reject(task, pending)
        cancel(db, task, APPROVAL_EXPIRED)
        cancelled.append(task.agent_id)
    db.commit()
    for agent_id in cancelled: settled(agent_id, "task_failed")
    return len(cancelled)

def cancel(db, task, reason):
    """Stop the task as task_ops.control does, with nobody told. The caller commits and calls settled()."""
    from personal_agent.workspace.file_memory import record_task
    task.status, task.waiting_reason = "cancelled", reason
    task.checkpoint = {**(task.checkpoint or {}), "outcome": "dropped"}
    record_task(db, task, db.get(PersonalAgent, task.agent_id))
    db.execute(update(PersonalDeviceCommand).where(PersonalDeviceCommand.work_item_id == task.id,
        PersonalDeviceCommand.status == "queued").values(status="revoked"))
    # What an earlier run of it changed and was put back is still told.
    from personal_agent.scheduling import proactive_guard
    proactive_guard.tell(db, task)

def _reject(task, pending):
    """What approvals.approve leaves for a rejection: the action can no longer be approved or asked again."""
    from personal_agent.tasks.approvals import intent
    state = dict(task.checkpoint or {})
    state.pop("pending_approval", None)
    state["rejected_actions"] = [*state.get("rejected_actions", []), {"action": intent(pending.get("action") or {})}][-20:]
    task.checkpoint = state

def revoked(db, agent):
    """True when the owner's agent is paused or may no longer use Personal Agent: a running proactive task stops."""
    from personal_agent import access
    return agent.status != "ready" or not access.allowed(db, agent.user_id)
