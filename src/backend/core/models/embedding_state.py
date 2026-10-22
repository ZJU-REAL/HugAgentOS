"""Embedding request limits and dimension cache, shared across consumers."""
from __future__ import annotations
import os
EMBED_DIMS = int(os.getenv("MEM0_EMBED_DIMS", "1024"))

EMBED_BATCH_SIZE = max(1, int(os.getenv("KB_EMBED_BATCH_SIZE", "32")))
EMBED_TIMEOUT = max(1, int(os.getenv("KB_EMBED_TIMEOUT", "120")))


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class EmbeddingStateState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    DETECTED_DIM_CACHE: dict[tuple[str, str], int] = state_field(default_factory=lambda: {})

@state_cache(maxsize=1)
def process_state() -> EmbeddingStateState:
    return EmbeddingStateState()

# Initialize under the import lock before concurrent callers share the owner.
process_state()
