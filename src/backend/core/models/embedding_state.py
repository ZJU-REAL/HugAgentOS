"""Embedding request limits and dimension cache, shared across consumers."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
EMBED_DIMS = int(os.getenv("MEM0_EMBED_DIMS", "1024"))

EMBED_BATCH_SIZE = max(1, int(os.getenv("KB_EMBED_BATCH_SIZE", "32")))
EMBED_TIMEOUT = max(1, int(os.getenv("KB_EMBED_TIMEOUT", "120")))


@dataclass
class EmbeddingState:
    DETECTED_DIM_CACHE: dict[tuple[str, str], int] = field(default_factory=lambda: {})
    # Monotonic time of the last failed dimension probe per (url, model): while it is
    # fresh, callers get the fallback dim at once instead of waiting on a dead endpoint.
    FAILED_DIM_PROBE_AT: dict[tuple[str, str], float] = field(default_factory=lambda: {})


# Seconds a failed dimension probe is remembered before the endpoint is tried again.
DIM_PROBE_RETRY_SECONDS = 30.0


_STATE = EmbeddingState()


def process_state() -> EmbeddingState:
    return _STATE
