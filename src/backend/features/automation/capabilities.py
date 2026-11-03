"""A scheduled task's capability selection, read once per run."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class TaskCapabilities:
    """What the task narrows the run's capabilities to.

    A field is None when the task did not choose: the run then uses the owner's
    current defaults, which the project environment narrows exactly like an
    interactive chat. These are available capabilities, never per-turn
    invocations, so an unavailable one is filtered out instead of failing the run.
    """

    mcp_ids: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None
    kb_ids: Optional[List[str]] = None
    agent_ids: Optional[List[str]] = None

    @classmethod
    def of(cls, task) -> "TaskCapabilities":
        # Storage keeps an unchosen selection as an empty list.
        return cls(
            mcp_ids=list(task.enabled_mcp_ids or []) or None,
            skill_ids=list(task.enabled_skill_ids or []) or None,
            kb_ids=list(task.enabled_kb_ids or []) or None,
            agent_ids=list(task.enabled_agent_ids or []) or None,
        )
