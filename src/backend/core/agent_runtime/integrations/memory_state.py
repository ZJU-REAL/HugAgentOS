"""memory state responsibilities."""
from __future__ import annotations
import weakref
import asyncio
from core.memory.retrieval_types import MemoryRetrievalResult










IDENTITY_CACHE_TTL_S = 60.0


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class MemoryStateState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    last_retrieval: 'weakref.WeakKeyDictionary[asyncio.Task, MemoryRetrievalResult]' = state_field(default_factory=lambda: weakref.WeakKeyDictionary())
    retrieval_states: 'weakref.WeakKeyDictionary[asyncio.Task, str]' = state_field(default_factory=lambda: weakref.WeakKeyDictionary())
    retrieval_budgets: 'weakref.WeakKeyDictionary[asyncio.Task, float]' = state_field(default_factory=lambda: weakref.WeakKeyDictionary())

@state_cache(maxsize=1)
def process_state() -> MemoryStateState:
    return MemoryStateState()

process_state()
