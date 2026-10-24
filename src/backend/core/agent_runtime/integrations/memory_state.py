"""Per-task memory retrieval results, states and budgets shared within one process."""
from __future__ import annotations
import weakref
import asyncio
from dataclasses import dataclass, field
from core.memory.retrieval_types import MemoryRetrievalResult


IDENTITY_CACHE_TTL_S = 60.0


@dataclass
class MemoryState:
    last_retrieval: 'weakref.WeakKeyDictionary[asyncio.Task, MemoryRetrievalResult]' = field(default_factory=lambda: weakref.WeakKeyDictionary())
    retrieval_states: 'weakref.WeakKeyDictionary[asyncio.Task, str]' = field(default_factory=lambda: weakref.WeakKeyDictionary())
    retrieval_budgets: 'weakref.WeakKeyDictionary[asyncio.Task, float]' = field(default_factory=lambda: weakref.WeakKeyDictionary())


_STATE = MemoryState()


def process_state() -> MemoryState:
    return _STATE
