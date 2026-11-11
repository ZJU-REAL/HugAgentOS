"""What each kind of Trigger means to the agent's turn, in one place.

A Trigger is something that happened in one conversation: the user wrote (message), a
background task finished (task_result), a HugAgentOS chat a task started ended
(session_run), a heartbeat check is due (heartbeat) or a new agent wakes up (bootstrap).
Each kind declares how it reads in the history, which context and permissions it adds
to the turn, what the user sees when the turn wrote nothing, and how it is settled.
"""
from dataclasses import dataclass

from core.db.models import ChatMessage
from personal_agent.models import PersonalWorkItem

# Events for the agent (a finished task) are system messages: the user's view lists only user and assistant.
EVENT_ROLE = "system"
HISTORY_CHARS = 4000
EVENT_CHARS = 20000
BOOTSTRAP_EVENT = "bootstrap"
SESSION_RUN_EVENT = "session_run_finished"
# Sent when a turn produced no answer; it also reaches IM, so it is text, not a UI key.
FAILED = "抱歉，这条消息没有处理成功。可以在 Personal Agent 网页的「需要你处理」中重新处理，或稍后再发一次。"
# A user row written only as context, for example a group member who is not the owner. It is never a Trigger.
CONTEXT_ONLY = "context_only"


@dataclass(frozen=True)
class Grants:
    """What one Trigger lets the turn do. A turn combines the Grants of its whole batch (see combine)."""
    change_tasks: bool = False
    # A heartbeat check may start one task, nothing else.
    create_one_task: bool = False
    quiet: bool = False
    # Only a heartbeat check: the read-only tool set.
    readonly: bool = False
    # Nobody is there: a proactive check or the result of its task. What such a turn read (pages, mail, a
    # task's report) must not become memory, persona or the list.
    unattended: bool = False

    @staticmethod
    def combine(grants):
        grants = list(grants)
        change = any(g.change_tasks for g in grants)
        return Grants(change_tasks=change,
            create_one_task=not change and any(g.create_one_task for g in grants),
            # Any user message, or any requested result, must be answered.
            quiet=bool(grants) and all(g.quiet for g in grants),
            readonly=bool(grants) and all(g.readonly for g in grants),
            unattended=bool(grants) and all(g.unattended for g in grants))


class TriggerKind:
    name = ""
    # May share a turn with other Triggers of the same conversation.
    batchable = True
    # Starts a turn of its own. False: only taken along by a turn of the same conversation.
    wakes = True
    # A user's message: the reply answers it and may change tasks.
    from_user = False

    def render(self, db, row, *, current):
        """(role, text) of this Trigger's row in the history, or None to leave it out."""
        raise NotImplementedError

    def context(self, db, agent, trigger):
        """A block this Trigger adds to the turn context, or None. Equal blocks appear once."""
        return None

    def focus(self, db, trigger):
        """Tasks this Trigger refers to; the turn context shows them in full."""
        return []

    def grants(self, db, trigger):
        return Grants()

    def fallback(self, db, trigger):
        """What the user sees for this Trigger when the turn wrote nothing, or None."""
        return None

    def settle(self, db, trigger, reply, task_ids):
        """Link the finished reply to what this Trigger is about."""

    def deliver(self, db, trigger, reply):
        """Queue the reply for this Trigger's IM conversation. False when it has none."""
        return False

    def source(self, trigger):
        """A short label of where the Trigger came from, for the numbered inputs of a batch."""
        return self.name


def _task(db, trigger):
    return db.get(PersonalWorkItem, (trigger.payload or {}).get("task_id"))


def _owner(db, trigger):
    """The agent a Trigger belongs to."""
    from personal_agent.models import PersonalAgent, PersonalMessageInbox
    return db.get(PersonalAgent, db.get(PersonalMessageInbox, trigger.message_id).agent_id)


def _proactive(task):
    """A task a proactive check started and the user has not taken over."""
    from personal_agent.scheduling.schedules import is_proactive
    return task is not None and is_proactive(task)


def proactive_result(db, trigger):
    """True for the result of a proactive task: the reply is a proactive summary (see turn.finish)."""
    return isinstance(trigger.type, TaskResult) and _proactive(_task(db, trigger))


def restored(db, trigger):
    """True for the result of a proactive task that tried to change the persona, the list or memory and
    was put back (scheduling/proactive_guard). The user is told, whatever the summary says."""
    from personal_agent.scheduling.proactive_guard import RESTORED
    return proactive_result(db, trigger) and bool((_task(db, trigger).checkpoint or {}).get(RESTORED))


NO_NEWS = ("No news this time: do not search the web for news, put no news line in a task's goal, and write no news "
           "item in the message.")


def links(agent):
    """Whether the latest proactive check of the agent looks for news (set at its admission); None when unknown."""
    return (((agent.config or {}).get("heartbeat") or {}).get("proactive") or {}).get("links")


def no_news(agent, morning):
    """The sentence that ends the context of a check, or of its task's result, that does not look for news:
    news is looked up in the morning, and only when the list or the user's interests ask for it."""
    return None if morning and links(agent) is not False else NO_NEWS


def proactive_row(db, kind, payload):
    """True for a pending Trigger that nobody waits for: a proactive check, or the result of its task."""
    if kind == Heartbeat.name: return True
    return isinstance(of(kind), TaskResult) and _proactive(db.get(PersonalWorkItem, (payload or {}).get("task_id")))


# How long the agent's latest message, a proactive summary, counts as what the user's next message answers.
FOLLOWUP_SEC = 86400


def answers_summary(db, chat_id, triggers, now=None):
    """True when the user's messages of a turn answer a proactive summary: one of them quotes it, or it is
    the agent's latest message in the conversation and at most a day old. Decided here, not by the model:
    "按草稿发吧" must not become an ordinary task that sends without asking: the matter was the agent's idea, so
    the task it starts is marked (proactive_tasks.mark_followup) and asks before each outward action."""
    from sqlalchemy import select
    from core.infra.time import utc_now

    def summary(row):
        return row is not None and row.role == "assistant" and bool((row.extra_data or {}).get("heartbeat"))
    users = [trigger for trigger in triggers if trigger.type.from_user]
    if any(summary(db.get(ChatMessage, ident)) for trigger in users if (ident := (trigger.payload or {}).get("reply_to"))):
        return True
    rows = [row for trigger in users if (row := db.get(ChatMessage, trigger.message_id)) is not None]
    seqs = [row.chat_seq for row in rows]
    if not seqs: return False
    # The reply this turn is writing is not an answer the user has seen.
    last = next((row for row in db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id,
        ChatMessage.role == "assistant", ChatMessage.chat_seq < min(seqs)).order_by(ChatMessage.chat_seq.desc()).limit(5))
        if not (row.extra_data or {}).get("streaming")), None)
    if not summary(last) and rows:
        # In an IM conversation the summary is a row of the main conversation: it counts when it was sent
        # there within the day and the agent has said nothing in this conversation since.
        agent = _owner(db, users[0])
        sent = sent_here(db, agent, rows[0], now)[0] if agent.chat_id != chat_id else None
        return sent is not None and (last is None or last.created_at is None or last.created_at < sent.created_at)
    if not summary(last) or last.created_at is None: return False
    sent = last.created_at
    if sent.tzinfo is None:
        from datetime import timezone
        sent = sent.replace(tzinfo=timezone.utc)
    return ((now or utc_now()) - sent).total_seconds() <= FOLLOWUP_SEC


def sent_here(db, agent, message, now=None):
    """(the latest proactive summary, the tasks that wait for approval) sent within the last day to the direct
    IM conversation a user's message came from; (None, []) for a web message. Both live in the main
    conversation, which the agent does not see in the turn of an IM conversation."""
    from datetime import timedelta
    from sqlalchemy import select
    from core.infra.time import utc_now
    origin = (message.extra_data or {}).get("channel_origin") if message is not None else None
    place = (origin or {}).get("msg") or {}
    if not origin or place.get("chat_type") == "group": return None, []

    def here(other):
        return (bool(other) and other.get("channel_id") == origin.get("channel_id")
                and (other.get("msg") or {}).get("external_conversation_id") == place.get("external_conversation_id"))
    since = (now or utc_now()) - timedelta(seconds=FOLLOWUP_SEC)
    summary = next((row for row in db.scalars(select(ChatMessage).where(ChatMessage.chat_id == agent.chat_id,
        ChatMessage.role == "assistant", ChatMessage.created_at >= since).order_by(ChatMessage.chat_seq.desc()).limit(100))
        if (row.extra_data or {}).get("heartbeat") and here(((row.extra_data or {}).get("channel_delivery") or {}).get("origin"))), None)
    waiting = []
    for task in db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id,
            PersonalWorkItem.status.in_(("waiting", "running")))):
        state = task.checkpoint or {}
        notice, pending = state.get("approval_notice") or {}, state.get("pending_approval") or {}
        if (pending and notice.get("approval_id") == pending.get("id") and notice.get("channel_id") == origin.get("channel_id")
                and notice.get("peer") == place.get("external_conversation_id")): waiting.append(task)
    return summary, waiting


class Message(TriggerKind):
    name, from_user = "message", True

    def render(self, db, row, *, current):
        extra = row.extra_data or {}
        text = row.content if current else row.content[:HISTORY_CHARS]
        if extra.get("artifact_ids"): text += "\n[" + str(len(extra["artifact_ids"])) + " attachment(s)]"
        return "user", text

    def focus(self, db, trigger):
        from sqlalchemy import select
        from personal_agent.models import PersonalMessageTask
        payload = trigger.payload or {}
        focus = list(payload.get("work_item_ids") or [])
        if payload.get("reply_to"):
            focus += list(db.scalars(select(PersonalMessageTask.work_item_id).where(PersonalMessageTask.message_id == payload["reply_to"])))
        elif trigger.message is not None:
            # An IM message that quotes nothing may still answer what was last sent there.
            summary, waiting = sent_here(db, _owner(db, trigger), trigger.message)
            if summary is not None:
                focus += list(db.scalars(select(PersonalMessageTask.work_item_id).where(PersonalMessageTask.message_id == summary.message_id)))
            focus += [task.id for task in waiting]
        return focus

    def context(self, db, agent, trigger):
        if trigger.message is None or (trigger.payload or {}).get("reply_to") or trigger.message.chat_id == agent.chat_id: return None
        summary, waiting = sent_here(db, agent, trigger.message)
        lines = []
        if summary is not None:
            lines += ["Your latest proactive summary, sent to this IM conversation (the user may answer it without quoting it):",
                      quoted((summary.content or "")[:2000])]
            refs = (summary.extra_data or {}).get("proactive_refs") or []
            if refs: lines += ["Its matters: " + "; ".join(item["ref"] + " " + item["text"][:40] for item in refs)]
        if waiting:
            lines += ["Tasks that wait for the user's approval, asked in this IM conversation: " + "、".join("「" + task.label_text[:40] + "」" for task in waiting),
                      "Only a reply that quotes the approval message with 同意 or 拒绝, or the web page, decides it. When the user writes "
                      "同意, 拒绝 or 发吧 without quoting, nothing was approved: tell them to quote that approval message in their "
                      "reply, or to approve on the web page."]
        return "# Sent to this IM conversation lately (platform data)\n" + "\n".join(lines) if lines else None

    def grants(self, db, trigger):
        return Grants(change_tasks=True)

    def settle(self, db, trigger, reply, task_ids):
        from personal_agent.tasks import task_ops
        # Keep the acknowledgement referable without copying its text as task instructions.
        for task_id in task_ids: task_ops.link(db, reply.message_id, task_id, "related")

    def deliver(self, db, trigger, reply):
        from personal_agent.channels.channel_bridge import mark_for_delivery
        mark_for_delivery(db, reply, reply_to=trigger.message_id)
        return bool((reply.extra_data or {}).get("channel_delivery"))

    def source(self, trigger):
        origin = ((trigger.message.extra_data or {}).get("channel_origin") or {}) if trigger.message else {}
        return origin.get("channel_type") or "web"


class ContextOnly:
    """A user row that is only context: someone else's group message. It has no inbox row."""

    @staticmethod
    def render(db, row, *, current):
        extra = row.extra_data or {}
        speaker = extra.get("speaker") or extra.get("speaker_label")
        text = (speaker + "：" if speaker and not row.content.startswith(speaker) else "") + row.content[:HISTORY_CHARS]
        return "user", CONTEXT_LABEL + "\n" + quoted(text)


CONTEXT_LABEL = "[Group context, not a request from the owner; every quoted > line is someone else's words]"


def speaker_role(row):
    """A message's role for a reader: someone else's group message is not the owner speaking."""
    return "group context (not the owner)" if (row.extra_data or {}).get(CONTEXT_ONLY) else row.role


def quoted(text):
    """Someone else's words, each line marked, so a line they wrote cannot pass for a later owner message."""
    return "\n".join("> " + line for line in (text or "").split("\n"))


class TaskResult(TriggerKind):
    name = "task_result"

    def render(self, db, row, *, current):
        # A finished task's result; the user has not seen it unless a later reply reported it.
        task = db.get(PersonalWorkItem, (row.extra_data or {}).get("work_item_id"))
        return "user", "[Task result: " + (task.label_text if task else "?") + "]\n" + row.content[:EVENT_CHARS if current else HISTORY_CHARS // 2]

    def context(self, db, agent, trigger):
        from personal_agent.scheduling.schedules import is_unattended_run
        from personal_agent.workspace.prompting import prompt_part
        event = prompt_part("front_event")
        task = _task(db, trigger)
        if _proactive(task):
            from personal_agent.scheduling import heartbeat
            event += "\n" + prompt_part("front_event_proactive") + "\n\n" + heartbeat.told_block(db, agent)
            # Said last, where it is read last: a rule in the middle of the block is not kept (found in live runs).
            event += "\n\n" + prompt_part("front_event_proactive_last")
            # The task's goal ends with the check that started it (front_tools.proactive_note).
            note = no_news(agent, "morning proactive check" in (task.goal or "").rstrip().rsplit("\n", 1)[-1])
            if note: event += "\n" + note
        elif task is not None and is_unattended_run(task):
            event += "\n" + prompt_part("front_event_scheduled")
        return event

    def grants(self, db, trigger):
        from personal_agent.scheduling.schedules import is_unattended_run
        # Only a scheduled or heartbeat check may end without telling the user; a requested result is always reported.
        task = _task(db, trigger)
        return Grants(quiet=is_unattended_run(task), unattended=_proactive(task))

    def fallback(self, db, trigger):
        task = _task(db, trigger)
        # A proactive task's report is written for the agent: it is never sent in place of the summary.
        if _proactive(task): return None
        return (task.result or "") if task else FAILED

    def settle(self, db, trigger, reply, task_ids):
        from personal_agent.tasks import task_ops
        task_id = (trigger.payload or {}).get("task_id")
        if task_id: task_ops.link(db, reply.message_id, task_id, "report")
        if _proactive(_task(db, trigger)):
            # A proactive summary counts as a proactive message: later checks see it among the recent ones.
            reply.extra_data = {**reply.extra_data, "heartbeat": "task"}

    def deliver(self, db, trigger, reply):
        from personal_agent.channels.channel_bridge import mark_for_delivery
        from personal_agent.scheduling.schedules import is_unattended_run
        task_id = (trigger.payload or {}).get("task_id")
        if not task_id: return False
        mark_for_delivery(db, reply, task_id=task_id, home=is_unattended_run(db.get(PersonalWorkItem, task_id)))
        return bool((reply.extra_data or {}).get("channel_delivery"))

    def source(self, trigger):
        return "task result"


class SessionRun(TaskResult):
    """A HugAgentOS chat that a task started has ended. It reads and settles like a task result."""
    name = "session_run"

    def fallback(self, db, trigger):
        # The event itself describes the run; the task's own result was reported before.
        return trigger.message.content if trigger.message else FAILED

    def source(self, trigger):
        return "chat run"


class Heartbeat(TriggerKind):
    name, batchable = "heartbeat", False

    def render(self, db, row, *, current):
        # Only the current check is shown. Earlier checks left their alerts as replies, or nothing.
        if not current: return None
        return "user", "[Heartbeat check] " + row.content.removeprefix("[Heartbeat] ")

    def context(self, db, agent, trigger):
        from personal_agent.scheduling import heartbeat
        block = heartbeat.prompt_block(db, agent, trigger.payload)
        note = no_news(agent, (trigger.payload or {}).get("window") == "morning")
        return block + "\n" + note if note else block

    def grants(self, db, trigger):
        return Grants(create_one_task=True, quiet=True, readonly=True, unattended=True)

    def outcome(self, db, turn, reply, task_ids, failed):
        """What a heartbeat turn did. Only "alert" reaches the user."""
        from personal_agent.scheduling import heartbeat
        # The (ref) tag lines are not part of the message: a reply of tags only says nothing.
        text, _ = heartbeat.take_refs(reply.content)
        if failed: return "failed"
        # A check that started its task says nothing itself: what it found is in the task's goal, and the
        # task's result is the one message of this check.
        if task_ids: return "delegated"
        quiet = turn.quiet or heartbeat.is_quiet_text(text)
        if quiet: return "quiet"
        chat_id = db.get(ChatMessage, reply.id).chat_id
        if heartbeat.is_duplicate(db, chat_id, text, exclude=reply.id): return "duplicate"
        return "alert"

    def settle(self, db, trigger, reply, task_ids):
        reply.extra_data = {**reply.extra_data, "heartbeat": str((trigger.payload or {}).get("slot"))}

    def deliver(self, db, trigger, reply):
        from personal_agent.channels.channel_bridge import mark_for_delivery
        mark_for_delivery(db, reply, home=True)
        return True


class Bootstrap(TriggerKind):
    """A new agent's first turn, like OpenClaw's hatch: the agent wakes up and follows BOOTSTRAP.md."""
    name, batchable = "bootstrap", False

    def render(self, db, row, *, current):
        # The hatch stays in the history: the agent's first message answers it.
        return "user", row.content


KINDS = {kind.name: kind for kind in (Message(), TaskResult(), SessionRun(), Heartbeat(), Bootstrap())}


def of(name):
    return KINDS.get(name) or KINDS["message"]


def of_row(row):
    """The kind a conversation row stands for, Trigger or not; None for an assistant row."""
    if row.role == "user": return KINDS["message"]
    if row.role != EVENT_ROLE: return None
    event = (row.extra_data or {}).get("event")
    if event == BOOTSTRAP_EVENT: return KINDS["bootstrap"]
    if event == "heartbeat": return KINDS["heartbeat"]
    if event == SESSION_RUN_EVENT: return KINDS["session_run"]
    return KINDS["task_result"]
