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

    @staticmethod
    def combine(grants):
        grants = list(grants)
        change = any(g.change_tasks for g in grants)
        return Grants(change_tasks=change,
            create_one_task=not change and any(g.create_one_task for g in grants),
            # Any user message, or any requested result, must be answered.
            quiet=bool(grants) and all(g.quiet for g in grants),
            readonly=bool(grants) and all(g.readonly for g in grants))


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
        return focus

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
        if task is not None and is_unattended_run(task): event += "\n" + prompt_part("front_event_scheduled")
        return event

    def grants(self, db, trigger):
        from personal_agent.scheduling.schedules import is_unattended_run
        # Only a scheduled or heartbeat check may end without telling the user; a requested result is always reported.
        return Grants(quiet=is_unattended_run(_task(db, trigger)))

    def fallback(self, db, trigger):
        task = _task(db, trigger)
        return (task.result or "") if task else FAILED

    def settle(self, db, trigger, reply, task_ids):
        from personal_agent.tasks import task_ops
        task_id = (trigger.payload or {}).get("task_id")
        if task_id: task_ops.link(db, reply.message_id, task_id, "report")

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
        return heartbeat.prompt_block(db, agent)

    def grants(self, db, trigger):
        return Grants(create_one_task=True, quiet=True, readonly=True)

    def outcome(self, db, turn, reply, task_ids, failed):
        """What a heartbeat turn did. Only "alert" reaches the user."""
        from personal_agent.scheduling import heartbeat
        text = reply.content
        if failed: return "failed"
        quiet = turn.quiet or heartbeat.is_quiet_text(text)
        if task_ids and quiet: return "delegated"
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
