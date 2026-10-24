"""Transactional command terminal-event admission and retry."""
import time
from copy import deepcopy
from sqlalchemy import update
from core.db.models import ChatRun, ChatSession
from core.db.models.command import CommandTask
from core.sandbox.commands import store
from core.chat.sequencer import ChatSequencer

def notice_text(rows):
    facts = ["[Background command completion]",
             "Handle each completion once. If its terminal result is already in this conversation, do not handle it or reply again.",
             "Otherwise read the task with write_stdin, then continue the original request. Do not restart these commands."]
    facts.extend(f"task_id={r['task_id']} status={r['status']} exit_code={r['exit_code']}" for r in rows)
    return "\n".join(facts)

def acknowledge(task_ids, run_id):
    with store.SessionLocal() as db:
        db.execute(update(CommandTask).where(CommandTask.task_id.in_(task_ids),
            CommandTask.notice_status == "reserved", CommandTask.notice_run_id == run_id)
            .values(notice_status="delivered", lease_until=0))
        db.commit()

def reserve_boundary(chat_id, user_id, run_id):
    """Persist facts and their receipt together before injecting live context."""
    from core.sandbox.commands.context import completion_context
    with store.SessionLocal() as db:
        db.execute(update(ChatSession).where(ChatSession.chat_id == chat_id)
                   .values(updated_at=ChatSession.updated_at))
        if not store.can_edit(db, chat_id, user_id):
            raise PermissionError("Conversation is not writable")
        rows = db.query(CommandTask).filter_by(chat_id=chat_id, user_id=user_id,
            notice_status="pending").order_by(CommandTask.task_id).limit(50).all()
        if not rows:
            db.rollback()
            return []
        facts = [store.view(row) for row in rows]
        run = db.get(ChatRun, run_id)
        if (run is None or run.chat_id != chat_id or run.user_id != user_id
                or (run.recovery_snapshot or {}).get("kind") != "chat"):
            raise RuntimeError("Active command recipient has no matching chat recovery context")
        snapshot = deepcopy(run.recovery_snapshot)
        # This is a sideband event inside the active assistant step. A separate
        # ChatMessage would sort AFTER the assistant's preallocated chat_seq.
        snapshot["worker_args"]["session_messages"].extend(
            completion_context(fact) for fact in facts)
        run.recovery_snapshot = snapshot
        run.snapshot_version = int(run.snapshot_version or 0) + 1
        for row in rows:
            row.notice_status, row.notice_run_id = "delivered", run_id
            row.delivery_mode = "boundary"
        db.commit()
        return facts

def _admission(task_ids, owner):
    def attach(db, run):
        changed = db.execute(update(CommandTask).where(CommandTask.task_id.in_(task_ids),
            CommandTask.notice_status == "pending").values(
                notice_status="reserved", notice_run_id=run.run_id, delivery_mode="followup",
                owner=owner, lease_until=time.time() + 30))
        if changed.rowcount != len(task_ids):
            raise RuntimeError("Command completion was already claimed")
    return attach

def prepare(rows, owner):
    from core.llm.context.ir_constants import KIND_REMINDER, POLICY_NEVER, SESSION_CONTEXT_META_KEY
    from core.llm.context.items import make_text_context_item, session_context_metadata
    first = rows[0]
    with store.SessionLocal() as db:
        chat = db.get(ChatSession, first["chat_id"])
        if not store.can_edit(db, first["chat_id"], first["user_id"]):
            db.execute(update(CommandTask).where(CommandTask.task_id.in_([r["task_id"] for r in rows]))
                       .values(notice_status="consumed"))
            db.commit()
            return None
        if ChatSequencer(db).get_active_run(chat.chat_id):
            return None
        source = db.get(ChatRun, first["source_run_id"]) if first["source_run_id"] else None
        if source is None or not source.recovery_snapshot:
            return None
        context = source.recovery_snapshot["worker_args"]["context"]
        prompt = notice_text(rows)
        item = make_text_context_item(prompt, item_id="harness:command_completion",
            kind=KIND_REMINDER, origin="harness:command_completion", trust="system",
            created_seq=0, priority=950, token_budget=4000, truncation_policy=POLICY_NEVER)
        accepted = ChatSequencer(db).accept_main_run(chat_id=chat.chat_id, user_id=first["user_id"],
            user_content=prompt, user_extra_data={"hidden_in_chat": True,
                "system_wake": "command_finish", SESSION_CONTEXT_META_KEY: session_context_metadata(item)},
            request_payload={"kind": "chat", "wake_reason": "command_finish",
                "chat_id": chat.chat_id,
                "chat_mode": context["chat_mode"],
                "enable_thinking": context["enable_thinking"]},
            on_accept=_admission([r["task_id"] for r in rows], owner))
        return accepted.run.run_id

def claim_retry(task_id, owner):
    with store.locked(task_id) as (db, row):
        if row.notice_status != "reserved" or row.delivery_mode != "followup" or row.lease_until > time.time():
            return None
        row.owner, row.lease_until = owner, time.time() + 30
        return row.notice_run_id

def load_launch(run_id):
    from core.chat.service import ChatService
    from core.chat.operations.session_context import load_session_messages as _load_session_messages
    with store.SessionLocal() as db:
        rows = db.query(CommandTask).filter_by(notice_run_id=run_id, notice_status="reserved").all()
        if not rows:
            return None
        run = db.get(ChatRun, run_id)
        if run is None:
            raise RuntimeError("Completion run disappeared")
        if run.recovery_snapshot:
            facts = [store.view(r) for r in rows]
            db.expunge(run)
            return facts, run, None, None
        source = db.get(ChatRun, rows[0].source_run_id)
        if source is None or not source.recovery_snapshot:
            raise RuntimeError("Original run context unavailable")
        args = deepcopy(source.recovery_snapshot["worker_args"])
        history = _load_session_messages(ChatService(db), run.chat_id, run.user_id)
        facts = [store.view(r) for r in rows]
        db.expunge(run)
        return facts, run, history, args

def release_notice(task_id, owner):
    with store.locked(task_id) as (_, row):
        if row.owner == owner and row.notice_status == "reserved":
            row.lease_until = 0
