from core.db.artifact_scope import is_personal_artifact
"""Owner-scoped tasks operations."""
from sqlalchemy import select, update, case
from sqlalchemy.exc import IntegrityError
import uuid
import time
from core.db.models import ChatMessage, Artifact

from personal_agent.models import PersonalAgent, PersonalWorkItem, PersonalDeviceCommand, TITLE_LENGTH
from personal_agent.identity.config import TERMINAL
from personal_agent.tasks.views import task_view

class TaskService:
    def __init__(self, context):
        self.context = context
        self.db, self.user_id = context.db, context.user_id

    @property
    def chat_id(self):
        return self.context.chat_id

    def get(self, ident, lock=False):
        query = select(PersonalWorkItem).join(PersonalAgent).where(PersonalWorkItem.id == ident, PersonalAgent.user_id == self.user_id)
        if lock: query = query.with_for_update()
        row = self.db.scalar(query)
        if row is None: raise LookupError("Task not found")
        if self.chat_id and self.chat_id != self.context.identity.agent().chat_id:
            from personal_agent.conversation.conversations import task_chat
            if task_chat(self.db, self.context.identity.agent(), row) != self.chat_id: raise LookupError("Task not found")
        return row

    def attachments(self, ids):
        if len(ids) > 10: raise ValueError("At most ten attachments")
        for ident in ids:
            row = self.db.get(Artifact, ident)
            if row is None or row.user_id != self.user_id or row.deleted_at or not is_personal_artifact(row):
                raise LookupError("Attachment unavailable")
            if row.size_bytes > 20 * 1024 * 1024: raise ValueError("Attachment exceeds 20 MiB")
        return list(dict.fromkeys(ids))

    def submit(self, goal, request_key, artifact_ids=None, origin=None, title=None):
        if not goal.strip() or len(goal) > 20000: raise ValueError("Task must contain 1 to 20000 characters; attach long instructions as a file")
        self.context.identity.ensure()
        artifact_ids = self.attachments(artifact_ids or [])
        agent = self.context.identity.agent()
        old = self.db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id, PersonalWorkItem.request_key == request_key))
        if old:
            if old.goal != goal or (old.checkpoint or {}).get("initial_artifact_ids", []) != artifact_ids: raise ValueError("Idempotency key was used for another message")
            return task_view(old)
        if agent.status != "ready": raise ValueError("Personal Agent is paused")
        task = PersonalWorkItem(id="pa_task_" + uuid.uuid4().hex, agent_id=agent.id, goal=goal, request_key=request_key,
            checkpoint={"source_chat_id": self.context.conversation.id(), "initial_artifact_ids": artifact_ids, "input_artifact_ids": artifact_ids, **({"title": title[:TITLE_LENGTH]} if title else {})})
        self.db.add(task)
        try:
            self.db.flush()
            message = self.context.conversation.append(agent, "user", goal, task.id, request_key, relation="goal")
            message.extra_data = {**message.extra_data, "artifact_ids": artifact_ids, **({"origin": origin} if origin else {})}
            task.context_seq = message.chat_seq
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            old = self.db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id, PersonalWorkItem.request_key == request_key))
            if old is None or old.goal != goal or (old.checkpoint or {}).get("initial_artifact_ids", []) != artifact_ids: raise
            return task_view(old)
        return task_view(task)

    def list(self, limit=100):
        query = select(PersonalWorkItem).join(PersonalAgent).where(PersonalAgent.user_id == self.user_id)
        agent = self.context.identity.agent()
        if self.chat_id:
            scope = self.context.conversation.id()
            if scope != agent.chat_id:
                query = query.where(PersonalWorkItem.checkpoint["source_chat_id"].as_string() == scope)
        rows = self.db.scalars(query.order_by(case((PersonalWorkItem.status.in_(["queued", "running", "waiting", "computer_wait"]), 0), else_=1), PersonalWorkItem.created_at.desc()).limit(limit))
        return [task_view(row) for row in rows]

    def cancel(self, ident):
        row = self.get(ident, lock=True)
        if row.status not in TERMINAL:
            row.status = "cancelled"
            self.db.execute(update(PersonalDeviceCommand).where(PersonalDeviceCommand.work_item_id == ident, PersonalDeviceCommand.status == "queued").values(status="revoked"))
            from personal_agent.workspace.file_memory import record_task
            record_task(self.db, row, self.context.identity.agent())
            self.db.commit()
        return task_view(row)

    def steer(self, ident, text, request_key, artifact_ids=None):
        if not text.strip() or len(text) > 4000: raise ValueError("Task update must contain 1 to 4000 characters; attach long updates as a file")
        artifact_ids = self.attachments(artifact_ids or [])
        row = self.get(ident, lock=True)
        if row.status in TERMINAL: raise ValueError("Task already ended; send a new task")
        agent = self.context.identity.agent()
        from personal_agent.conversation.conversations import task_chat
        messages = self.db.scalars(select(ChatMessage).where(ChatMessage.chat_id == task_chat(self.db, agent, row), ChatMessage.role == "user", ChatMessage.extra_data["request_key"].as_string() == request_key).limit(1)).all()
        old = next((m for m in messages if (m.extra_data or {}).get("request_key") == request_key), None)
        if old:
            if old.content != text or (old.extra_data or {}).get("work_item_id") != ident or (old.extra_data or {}).get("artifact_ids", []) != artifact_ids: raise ValueError("Idempotency key conflict")
            return task_view(row)
        inputs = list(dict.fromkeys([*(row.checkpoint or {}).get("input_artifact_ids", []), *artifact_ids]))
        if len(inputs) > 10: raise ValueError("Task has more than ten input attachments")
        message = self.context.conversation.append(agent, "user", text, ident, request_key)
        message.extra_data = {**message.extra_data, "artifact_ids": artifact_ids}
        row.checkpoint = {**(row.checkpoint or {}), "input_artifact_ids": inputs}
        state = dict(row.checkpoint or {})
        state["approved_action_payloads"] = {}
        state["approved_actions"] = []
        state.pop("pending_approval", None)
        row.checkpoint = state
        self.db.commit()
        return task_view(row)

    def complete(self, ident, result, checkpoint, owner=None):
        row = self.get(ident, lock=True)
        if row.status in TERMINAL: return task_view(row)
        if owner is not None and (row.owner != owner or row.status != "running" or not row.lease_until or row.lease_until < time.time()): raise ValueError("Task lease lost")
        from personal_agent.tasks.steering import has_unread
        consumed = checkpoint.get("consumed_message_ids")
        if consumed is not None and has_unread(self.db, row, set(consumed)):
            # Preserve acknowledged inputs so the next execution only consumes unread instructions.
            row.checkpoint = {**checkpoint, **(row.checkpoint or {}),
                              "consumed_message_ids": list(consumed)}
            row.pop_state("final_answer")
            row.requeue()
            self.db.commit()
            return task_view(row)
        row.result = result
        row.checkpoint = {**checkpoint, **({"source_chat_id": row.checkpoint["source_chat_id"]} if (row.checkpoint or {}).get("source_chat_id") else {})}
        row.status = "completed"
        # The agent tells the user in the conversation; the full result stays on the task.
        from personal_agent.conversation.front import announce
        announce(self.context, row, result)
        self.db.commit()
        return task_view(row)

    def retry(self, ident):
        row = self.get(ident, lock=True)
        if row.status not in {"failed", "waiting"}: raise ValueError("Task is not waiting or failed")
        if (row.checkpoint or {}).get("pending_approval"): raise ValueError("Approve or reject the pending action first")
        if row.unconfirmed(): raise ValueError("Check uncertain actions before continuing")
        state = dict(row.checkpoint or {})
        state.pop("task_wait_reason", None)
        row.checkpoint = state
        row.requeue()
        self.db.commit()
        return task_view(row)
