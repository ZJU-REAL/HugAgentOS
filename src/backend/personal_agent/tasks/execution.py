"""Fence effects to one execution. Shared state survives child tool tasks."""
from contextvars import ContextVar
current_execution = ContextVar("personal_execution", default=None)

def bind(task):
    current_execution.set([task.id, task.run_id, task.owner, task.revision])

def consume(task):
    expected = current_execution.get()
    if expected is None:
        bind(task)
    elif expected[:3] != [task.id, task.run_id, task.owner]:
        raise PermissionError("Task execution changed")
    else:
        expected[3] = task.revision
