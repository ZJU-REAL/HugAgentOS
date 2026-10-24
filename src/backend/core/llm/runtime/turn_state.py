"""Per-turn trackers for artifact reads and pin hints."""
from __future__ import annotations
from contextvars import ContextVar
from typing import Any, Dict, Optional

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
    """Clear the per-turn read tracker. Called from file_context_pre_reply."""
    artifact_read_chars_per_turn.set({})


def reset_pin_hint_state() -> None:
    """Clear the per-turn pin-reminder cache. Called at turn boundary."""
    pin_hint_state.set({"seen": set(), "last_reminded_sig": ""})


def get_pin_hint_state() -> Dict[str, Any]:
    state = pin_hint_state.get()
    if state is None:
        state = {"seen": set(), "last_reminded_sig": ""}
        pin_hint_state.set(state)
    return state


artifact_read_chars_per_turn: ContextVar[Optional[Dict[str, int]]] = ContextVar(
    "_artifact_read_chars_per_turn", default=None
)


pin_hint_state: ContextVar[Optional[Dict[str, Any]]] = ContextVar("_pin_hint_state", default=None)
