"""Guards for Personal Agent."""
import time

from core.db.engine import SessionLocal

from personal_agent.models import PersonalAgent, PersonalWorkItem

PENDING = "An action waits for user approval. Stop now: call pa_finish with one sentence that says what needs approval."

def owned_task(task_id, user_id, *, allow_pending=False):
    with SessionLocal() as db:
        task = db.get(PersonalWorkItem, task_id)
        agent = db.get(PersonalAgent, task.agent_id) if task else None
        if not agent or agent.user_id != user_id or task.status != "running" or not task.lease_until or task.lease_until < time.time():
            raise PermissionError("Task is not active or owned")
        from personal_agent.tasks.execution import current_execution
        expected = current_execution.get()
        if expected and tuple(expected) != (task.id, task.run_id, task.owner):
            raise PermissionError("Task execution changed")
        # While an approval is pending, the model must stop. It must not look for a workaround.
        if not allow_pending and (task.checkpoint or {}).get("pending_approval"):
            raise PermissionError(PENDING)
        from personal_agent.conversation.conversations import task_chat
        chat_id = task_chat(db, agent, task)
        return {"run_id": task.run_id, "agent_id": agent.id, "box": agent.sandbox_session_id, "chat": chat_id, "generation": agent.generation, "owner": task.owner, "config": agent.config or {}}
