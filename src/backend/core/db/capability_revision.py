"""Cross-worker revision for capability configuration (MCP servers and skills).

Every API worker keeps its own MCP config and capability-catalog caches. Any ORM
write touching a watched table bumps one revision row inside the same
transaction, and readers compare it before serving cached data, so every worker
sees a committed change on its next lookup instead of after a TTL.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from sqlalchemy import event
from sqlalchemy.orm import Session

from core.db import config_revision

REVISION_KEY = "capability_config_revision"
WATCHED_TABLES = frozenset({"admin_mcp_servers", "admin_skills"})
_FLAG = "_capability_revision_bumped"
_CHECK_INTERVAL_S = 1.0

_lock = threading.Lock()
_observed: tuple[float, Optional[str]] = (0.0, None)


def read_revision(db) -> str:
    return config_revision.read_revision(db, REVISION_KEY)


def current_revision() -> Optional[str]:
    """The revision, read at most once a second per process (None if unreadable).

    Several caches consult it on every chat turn; one primary-key read a second
    keeps them cross-worker fresh without a database hit per lookup.
    """
    global _observed
    now = time.monotonic()
    checked_at, value = _observed
    if now - checked_at < _CHECK_INTERVAL_S:
        return value
    with _lock:
        checked_at, value = _observed
        if now - checked_at < _CHECK_INTERVAL_S:
            return value
        try:
            from core.db.engine import SessionLocal

            with SessionLocal() as db:
                value = read_revision(db)
        except Exception:  # noqa: BLE001 - unreadable revision: callers fall back to their TTL
            value = None
        _observed = (now, value)
        return value


def _expire_observed() -> None:
    global _observed
    _observed = (0.0, _observed[1])


def _table_name(obj) -> Optional[str]:
    return getattr(getattr(obj, "__table__", None), "name", None)


def _bump_once(session) -> None:
    if session.info.get(_FLAG):
        return
    # Connection-level execute: no autoflush recursion, same transaction.
    config_revision.bump_revision(session.connection(), REVISION_KEY, "capability_config")
    session.info[_FLAG] = True


@event.listens_for(Session, "before_flush")
def _bump_on_flush(session, _flush_context, _instances) -> None:
    if session.info.get(_FLAG):
        return
    touched = any(_table_name(o) in WATCHED_TABLES for o in session.new) or any(
        _table_name(o) in WATCHED_TABLES for o in session.deleted
    ) or any(_table_name(o) in WATCHED_TABLES and session.is_modified(o) for o in session.dirty)
    if touched:
        _bump_once(session)


@event.listens_for(Session, "do_orm_execute")
def _bump_on_bulk(state) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is not None and mapper.local_table.name in WATCHED_TABLES:
        _bump_once(state.session)


@event.listens_for(Session, "after_commit")
def _after_commit(session) -> None:
    # This worker's own write is visible to its next lookup immediately.
    if session.info.get(_FLAG):
        _expire_observed()


@event.listens_for(Session, "after_transaction_end")
def _reset(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_FLAG, None)
