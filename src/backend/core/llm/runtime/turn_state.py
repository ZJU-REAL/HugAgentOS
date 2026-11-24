"""Per-turn tracker for artifact reads."""
from __future__ import annotations
from contextvars import ContextVar
from typing import Dict, Optional

def get_artifact_read_state() -> Dict[str, int]:
    """Return the current per-turn read tracker, lazily initializing it.

    Used by ``read_artifact`` to enforce the cumulative cap.
    """
    state = artifact_read_chars_per_turn.get()
    if state is None:
        state = {}
        artifact_read_chars_per_turn.set(state)
    return state


def reset_artifact_read_state() -> None:
    """Clear the per-turn read tracker. Called at every turn boundary (``begin_context_turn``)."""
    artifact_read_chars_per_turn.set({})


artifact_read_chars_per_turn: ContextVar[Optional[Dict[str, int]]] = ContextVar(
    "_artifact_read_chars_per_turn", default=None
)
