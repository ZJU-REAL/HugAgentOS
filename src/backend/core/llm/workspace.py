"""Per-run workspace state for the ``pin_to_workspace`` tool.

Strict mode: artifacts produced by tools are **hidden by default**. The
only way a generated file reaches the assistant message (and therefore
the user) is for the agent to explicitly call ``pin_to_workspace`` with
that file_id. The system prompt + tool docstring make this requirement
explicit; the agent is expected to pin whenever a deliverable file is
produced.

This module exposes the per-chat-run state that ``pin_to_workspace``
mutates and ``chat_stream`` / ``chat_run_executor`` / ``automation_scheduler``
/ ``batch_orchestrator`` read at meta-emission time. ContextVars scope
the state per async-context, so concurrent chats don't collide and a
nested batch item's state doesn't leak back to its parent (callers can
use ``scope()`` for nested workflows).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional, TypedDict


class _PinnedItem(TypedDict, total=False):
    file_id: str
    name: Optional[str]
    mime_type: Optional[str]
    size: Optional[int]
    url: Optional[str]


class _WorkspaceState(TypedDict, total=False):
    pinned: List[_PinnedItem]
    seen: set  # set[str] — file_ids already pinned, for dedup
    active: bool  # True once pin_to_workspace has been invoked at all


_workspace_var: ContextVar[Optional[_WorkspaceState]] = ContextVar(
    "hugagent_workspace_state", default=None,
)


def init_state() -> _WorkspaceState:
    """Initialize a fresh workspace state in the current async context.

    Call once at the entry of each chat run (chat_stream, chat_run_executor,
    automation_scheduler) so subsequent pin_to_workspace calls land in this
    state. Returns the state dict so callers can read it later if they
    prefer to skip get_state().
    """
    state: _WorkspaceState = {"pinned": [], "seen": set(), "active": False}
    _workspace_var.set(state)
    return state


def get_state() -> Optional[_WorkspaceState]:
    return _workspace_var.get()


def is_active() -> bool:
    """True iff pin_to_workspace was invoked at least once this run.

    No longer used for gating (strict mode always gates) but kept for
    diagnostics and tests — it tells you whether the agent attempted any
    pin during this turn.
    """
    state = _workspace_var.get()
    return bool(state and state.get("active"))


def mark_active() -> None:
    state = _workspace_var.get()
    if state is not None:
        state["active"] = True


@contextmanager
def scope() -> Iterator[_WorkspaceState]:
    """Create a fresh workspace state for the body and restore on exit.

    Use in nested flows (e.g. each batch item) so the inner state doesn't
    leak back to the parent context. Top-level entry points
    (``chat_stream``, ``chat_run_executor``, ``automation_scheduler``)
    can use ``init_state()`` directly since they own the outermost scope.
    """
    state: _WorkspaceState = {"pinned": [], "seen": set(), "active": False}
    token = _workspace_var.set(state)
    try:
        yield state
    finally:
        _workspace_var.reset(token)


def pin(
    file_id: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
    size: Optional[int] = None,
    url: Optional[str] = None,
) -> bool:
    """Add a file to the workspace. Returns False on dedup or no active state."""
    state = _workspace_var.get()
    if state is None:
        return False
    fid = (file_id or "").strip()
    if not fid:
        return False
    seen: set = state.setdefault("seen", set())
    if fid in seen:
        return False
    seen.add(fid)
    state.setdefault("pinned", []).append({
        "file_id": fid,
        "name": name,
        "mime_type": mime_type,
        "size": size,
        "url": url,
    })
    return True


def get_pinned() -> List[Dict[str, Any]]:
    state = _workspace_var.get()
    if not state:
        return []
    # Strip Nones so the SSE meta event stays compact and frontend-shaped.
    out: List[Dict[str, Any]] = []
    for item in state.get("pinned", []):
        cleaned = {k: v for k, v in item.items() if v is not None}
        out.append(cleaned)
    return out


def get_pinned_file_ids() -> List[str]:
    state = _workspace_var.get()
    if not state:
        return []
    return [item["file_id"] for item in state.get("pinned", [])]
