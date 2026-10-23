"""Task input delivery using message identities, never task versions."""
import asyncio
from contextvars import ContextVar
import time
from core.db.engine import SessionLocal
from personal_agent.models import PersonalAgent, PersonalWorkItem
from personal_agent.conversation.associations import instruction_messages

current_steering = ContextVar("personal_steering", default=None)


class TaskSteering:
    def __init__(self, task_id, uid, run_id, owner, messages, artifact_ids=(), consumed_ids=()):
        self.task_id, self.uid, self.run_id, self.owner = task_id, uid, run_id, owner
        previous = set(consumed_ids)
        unread = [item for item in messages if item[0] not in previous]
        # Bound historical context, but never acknowledge omitted unread input.
        self.initial_messages = unread[:30] if unread else messages[-30:]
        self.consumed = previous | {ident for ident, _ in self.initial_messages}
        self.artifact_ids = set(artifact_ids)

    def hook_policy(self):
        from core.chat.steer_hook import SteeringHook
        return SteeringHook(self.take, self.acknowledge)

    def _task(self, db, *, lock=False):
        from sqlalchemy import select
        query = select(PersonalWorkItem).where(PersonalWorkItem.id == self.task_id)
        task = db.scalar(query.with_for_update() if lock else query)
        agent = db.get(PersonalAgent, task.agent_id) if task else None
        if (not agent or agent.user_id != self.uid or task.run_id != self.run_id
                or task.owner != self.owner or task.status != "running"
                or not task.lease_until or task.lease_until < time.time()):
            raise PermissionError("Task is not active or owned")
        return task, agent

    def _pending(self):
        with SessionLocal() as db:
            task, agent = self._task(db)
            from personal_agent.conversation.conversations import task_chat
            pending = instruction_messages(db, task_chat(db, agent, task), task.id,
                                           exclude=self.consumed, limit=10)
            inputs = set((task.checkpoint or {}).get("input_artifact_ids", []))
            return pending, inputs, agent.sandbox_session_id

    async def take(self, run_id):
        if run_id != self.run_id:
            raise PermissionError("Task execution changed")
        pending, inputs, box = await asyncio.to_thread(self._pending)
        if not pending:
            return None
        from personal_agent.tasks.attachments import stage_inputs
        attachments = await stage_inputs(self.task_id, self.uid, box, sorted(inputs - self.artifact_ids))
        return {"message": "\n\n".join(text for _, text in pending) + attachments,
                "message_ids": [ident for ident, _ in pending], "artifact_ids": sorted(inputs)}

    def _clear_answer(self):
        # A finish tool from the previous batch may have saved an answer. Once
        # new input enters context that answer must not override the next reply.
        with SessionLocal() as db:
            task, _ = self._task(db, lock=True)
            task.pop_state("final_answer")
            db.commit()

    async def acknowledge(self, delivery):
        await asyncio.to_thread(self._clear_answer)
        self.consumed.update(delivery["message_ids"])
        self.artifact_ids.update(delivery["artifact_ids"])


def has_unread(db, task, consumed):
    agent = db.get(PersonalAgent, task.agent_id)
    from personal_agent.conversation.conversations import task_chat
    return any(ident not in consumed for ident, _ in instruction_messages(
        db, task_chat(db, agent, task), task.id))
