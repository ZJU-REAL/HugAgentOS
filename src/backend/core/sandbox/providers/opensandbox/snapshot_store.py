"""Lazy access to snapshot persistence and snapshot lifecycle constants."""

from __future__ import annotations


def _get_db_session_local():
    from core.db.engine import SessionLocal

    return SessionLocal


def _get_snapshot_model():
    from core.db.models import ChatSandboxSnapshot

    return ChatSandboxSnapshot


# OpenSandbox snapshot lifecycle states (the status.state returned by GET /v1/snapshots/{id}).
# SDK 0.1.13 does not expose an enum, so we can only string-compare these values.
_SNAPSHOT_STATE_READY = "Ready"

_SNAPSHOT_TERMINAL_FAILURE_STATES = frozenset({"Failed", "Error"})
