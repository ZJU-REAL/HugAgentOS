"""Triggers: the durable inputs of the agent's turn, and the one way to admit them.

Every producer (a user message, a finished task, a finished HugAgentOS chat, a heartbeat
check, a new agent's hatch) writes its conversation row and calls enqueue. A Trigger only
says what happened; the Turn that claims it holds the lease (see scheduler.py).
"""
import uuid
from dataclasses import dataclass, field, replace

from sqlalchemy import select

from core.db.models import ChatMessage
from core.db.repository.chat import ChatMessageRepository
from personal_agent.conversation.router import kinds
from personal_agent.models import PersonalMessageInbox
from personal_agent.tasks import task_ops

# Statuses of a Trigger the agent has not finished.
OPEN = ("pending", "claimed")
# A long result is delivered as it is: rewriting it would cost a slow model call and risk losing content.
LONG_RESULT = 8000
HATCH = "醒醒，我的朋友！"


@dataclass
class Trigger:
    """One claimed Trigger as the running turn sees it. n is its 1-based position in the batch."""
    message_id: str
    kind: str
    payload: dict
    seq: int
    n: int = 1
    message: object = field(default=None, compare=False, repr=False)

    @property
    def type(self):
        return kinds.of(self.kind)

    def bound(self, db):
        """This Trigger with its conversation row, read in db."""
        return replace(self, message=db.get(ChatMessage, self.message_id))


def enqueue(db, agent, *, kind, message, request_key, payload):
    """The Trigger of a conversation row that the agent should answer. Idempotent by request_key
    (a unique constraint per agent); the caller commits."""
    row = PersonalMessageInbox(message_id=message.message_id, agent_id=agent.id, request_key=request_key[:100],
        kind=kinds.of(kind).name, chat_id=message.chat_id, payload=payload)
    db.add(row)
    return row


def claimed(db, turn_id):
    """The Triggers of one Turn, in conversation order."""
    rows = db.execute(select(PersonalMessageInbox, ChatMessage.chat_seq).join(ChatMessage, ChatMessage.message_id == PersonalMessageInbox.message_id)
        .where(PersonalMessageInbox.turn_id == turn_id).order_by(ChatMessage.chat_seq)).all()
    return [Trigger(row.message_id, row.kind, dict(row.payload or {}), seq, n) for n, (row, seq) in enumerate(rows, 1)]


def inbox_view(row):
    # A heartbeat check is not an answer the user waits for: the page shows no typing line for it.
    return {"message_id": row.message_id, "status": row.status, "heartbeat": row.kind == "heartbeat",
        "work_item_ids": (row.decision or {}).get("work_item_ids", [])}


def reprocess(db, row):
    """Answer a failed Trigger again. The others that failed in the same turn go back with it,
    so the next turn takes them together."""
    batch = [row]
    if row.turn_id:
        batch += db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == row.turn_id,
            PersonalMessageInbox.message_id != row.message_id, PersonalMessageInbox.status == "failed").with_for_update()).all()
    for item in batch:
        decision = dict(item.decision or {})
        item.status, item.turn_id = "pending", None
        item.decision = {**decision, "reprocessed": decision.get("reprocessed", 0) + 1}
    return batch


# ---------------------------------------------------------------- producers without a module of their own

def announce(svc, task, result):
    """A finished task reports to the agent, which tells the user in its own words.

    The result stays on the task (card and private conversation) in full. A long result,
    or one that the agent cannot rewrite, is posted to the user unchanged.
    """
    from personal_agent.workspace.file_memory import admit as admit_memory
    agent = svc.identity.agent()
    if len(result) > LONG_RESULT:
        message = svc.conversation.append(agent, "assistant", result, task.id)
        admit_memory(message, agent, "task_completed", input_text=task.goal)
        return
    from personal_agent.conversation.conversations import task_chat
    event = ChatMessageRepository(svc.db).create({"message_id": "pa_evt_" + uuid.uuid4().hex, "chat_id": task_chat(svc.db, agent, task),
        "role": kinds.EVENT_ROLE, "content": result, "extra_data": {"work_item_id": task.id, "event": "task_completed"}}, commit=False)
    admit_memory(event, agent, "task_completed", input_text=task.goal)
    # The result is the task's final message: its private conversation imports it from this link.
    task_ops.link(svc.db, event.message_id, task.id, "final", result)
    key = "event:" + task.id + ":" + (task.run_id or uuid.uuid4().hex)
    enqueue(svc.db, agent, kind="task_result", message=event, request_key=key,
        payload={"kind": "task_event", "task_id": task.id, "event": "completed"})
    svc.db.flush()
    from personal_agent.tasks.task_threads import sync_thread
    sync_thread(svc.db, task, svc.user_id)


def hatch(svc):
    """Wake a new agent once, as OpenClaw's hatch: its first turn follows BOOTSTRAP.md and greets the user.

    Admitted only into an empty conversation of a ready agent whose first run is pending. True when admitted."""
    from personal_agent.workspace.files import WorkspaceUnavailable, local_workspace
    from personal_agent.workspace.persona import ensure_seeded, settle_bootstrap
    from sqlalchemy.exc import IntegrityError
    agent = svc.identity.agent()
    if agent is None or agent.status != "ready": return False
    if svc.db.scalar(select(ChatMessage.message_id).where(ChatMessage.chat_id == agent.chat_id).limit(1)): return False
    try:
        local_workspace(agent.id)
        ensure_seeded(agent.id)
        if not settle_bootstrap(agent.id, agent.config): return False
    except (WorkspaceUnavailable, OSError):
        return False
    message = ChatMessageRepository(svc.db).create({"message_id": "pa_boot_" + uuid.uuid4().hex, "chat_id": agent.chat_id,
        "role": kinds.EVENT_ROLE, "content": HATCH, "extra_data": {"event": kinds.BOOTSTRAP_EVENT}}, commit=False)
    enqueue(svc.db, agent, kind="bootstrap", message=message, request_key=kinds.BOOTSTRAP_EVENT, payload={"kind": kinds.BOOTSTRAP_EVENT})
    try:
        svc.db.commit()
    except IntegrityError:
        # Another request hatched it.
        svc.db.rollback()
        return False
    return True
