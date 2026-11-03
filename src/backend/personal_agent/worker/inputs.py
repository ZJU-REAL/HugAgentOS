"""Task input for the Worker session, tracked by message identity.

An Input is a task update (``PersonalMessageTask(relation="update")``). It is handed to
the Worker once: steered into its live run, or written as the user message of its next
activation. ``checkpoint.consumed_message_ids`` lists the updates already handed over.
"""
from core.llm.context.ir_constants import KIND_REMINDER, POLICY_NEVER, SESSION_CONTEXT_META_KEY
from core.llm.context.items import make_text_context_item, session_context_metadata
from personal_agent.conversation.associations import instruction_messages

# At most this many updates enter one boundary or activation; the rest follow at the next one.
BATCH = 30


def unread(db, task, consumed=None):
    """``(message_id, text)`` of updates the Worker has not received, oldest first."""
    from personal_agent.conversation.conversations import task_chat
    from personal_agent.models import PersonalAgent
    seen = set((task.checkpoint or {}).get("consumed_message_ids", []) if consumed is None else consumed)
    agent = db.get(PersonalAgent, task.agent_id)
    return [item for item in instruction_messages(db, task_chat(db, agent, task), task.id) if item[0] not in seen]


def has_unread(db, task, consumed):
    return bool(unread(db, task, consumed))


def consume(task, message_ids):
    state = dict(task.checkpoint or {})
    state["consumed_message_ids"] = list(dict.fromkeys([*state.get("consumed_message_ids", []), *message_ids]))
    # A final report written before this input reached the Worker no longer answers the task.
    if (state.get("report") or {}).get("status") not in (None, "progress"):
        state.pop("report", None)
    task.checkpoint = state


def unconsume(task, message_ids):
    state = dict(task.checkpoint or {})
    drop = set(message_ids)
    state["consumed_message_ids"] = [ident for ident in state.get("consumed_message_ids", []) if ident not in drop]
    task.checkpoint = state


def text(updates):
    return "\n\n".join(body for _, body in updates)


def new_attachments(task):
    """Input attachments not yet copied into the task folder."""
    state = task.checkpoint or {}
    staged = set(state.get("staged_artifact_ids", []))
    return [ident for ident in state.get("input_artifact_ids", []) if ident not in staged]


def staged(task, artifact_ids):
    state = dict(task.checkpoint or {})
    state["staged_artifact_ids"] = list(dict.fromkeys([*state.get("staged_artifact_ids", []), *artifact_ids]))
    task.checkpoint = state


def context_metadata(content, item_id):
    """A fixed context item: the brief and notes are never truncated away by compaction."""
    item = make_text_context_item(content, item_id=item_id, kind=KIND_REMINDER, origin="personal_agent:task",
        trust="system", created_seq=0, priority=900, token_budget=8000, truncation_policy=POLICY_NEVER)
    return {SESSION_CONTEXT_META_KEY: session_context_metadata(item)}


def inject(db, task, content, *, kind="note"):
    """``inject`` delivery: written to the Worker's history without waking it (§6.2).

    A hidden user row with ``context_only``; the next activation reads it with the rest of the history."""
    from personal_agent.tasks.task_threads import append, sync_thread
    session = sync_thread(db, task, None)
    return append(db, session, "user", content, {"hidden_in_chat": True, "context_only": True,
        "personal_input": kind, "work_item_id": task.id, **context_metadata(content, "personal_agent:" + kind)})
