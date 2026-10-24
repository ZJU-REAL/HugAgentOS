"""Local worker handles, keyed by durable run identity."""

from __future__ import annotations

import asyncio
import typing as _typing

from core.infra.logging import get_logger

logger = get_logger(__name__)




from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class TasksState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    active_runs: _typing.Dict[str, asyncio.Task] = state_field(default_factory=lambda: {})

@state_cache(maxsize=1)
def process_state() -> TasksState:
    return TasksState()

# Initialize under the import lock before concurrent callers share the owner.
process_state()
