"""Fence effects to one execution, independently of incoming messages."""
from contextvars import ContextVar
current_execution = ContextVar("personal_execution", default=None)

def bind(task):
    current_execution.set((task.id, task.run_id, task.owner))
