"""core agent_runtime resources responsibilities."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)




from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class ResourcesState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    persistent_clients: list = state_field(default_factory=lambda: [])

@state_cache(maxsize=1)
def process_state() -> ResourcesState:
    return ResourcesState()

# Initialize under the import lock before concurrent callers share the owner.
process_state()
