"""This computer's own chats that changed, carried to the cloud by the next heartbeat.

The desktop's chats live in its local database, and the cloud reaches them only through
session operations. So that an open page need not ask again and again, the desktop notes
which chats each commit touched (messages, runs starting or ending, the list itself) and
the heartbeat reports them; the cloud then tells the owner's open pages
(:mod:`personal_agent.conversation.live`), which read only those chats.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Iterable, Optional, Tuple

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from core.infra.logging import get_logger

logger = get_logger(__name__)

# Reports carry at most this many chats; a longer list is cut and the chat list is re-read instead.
MAX_CHATS = 100
# Changes this close together go out in one report.
REPORT_GAP = 1.0
# A run renewing its lease, or a row only stamped with a new time, is not a change a page shows.
QUIET_COLUMNS = {"lease_owner", "lease_expires_at", "updated_at", "heartbeat_at"}
# The Personal Agent's own chats live in the cloud, never here.
CLOUD_PREFIX = "pa_chat_"

_lock = threading.Lock()
_chats: set = set()
_listed = False
_waiters: list = []
_installed = False
# Counts changes noted, so the heartbeat wakes early only for ones made after its last attempt.
_version = 0


def _note(chats: Iterable[str], listed: bool) -> None:
    global _listed, _version
    chats = {chat for chat in chats if chat and not chat.startswith(CLOUD_PREFIX)}
    if not chats and not listed:
        return
    with _lock:
        _chats.update(chats)
        _listed = _listed or listed
        _version += 1
        waiters = list(_waiters)
    for loop, ready in waiters:
        try:
            loop.call_soon_threadsafe(ready.set)
        except RuntimeError:  # pragma: no cover - the heartbeat's loop is gone
            continue


def take() -> Tuple[list, bool]:
    """The chats changed since the last report, and whether the list changed; forgotten once taken."""
    global _listed
    with _lock:
        chats, listed = sorted(_chats), _listed
        _chats.clear()
        _listed = False
    if len(chats) > MAX_CHATS:
        return chats[:MAX_CHATS], True
    return chats, listed


def restore(chats: Iterable[str], listed: bool) -> None:
    """A report that did not reach the cloud: keep its changes for the next one."""
    global _listed
    with _lock:
        _chats.update(chats)
        _listed = _listed or listed


def version() -> int:
    with _lock:
        return _version


async def wait(seconds: float, last_report: float, seen: int) -> None:
    """Sleep until the next heartbeat is due, or until a change newer than ``seen`` is noted.

    Changes a failed report put back do not wake it: they wait for the next regular heartbeat,
    so an unreachable cloud is not asked more often.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    ready = asyncio.Event()
    waiter = (loop, ready)
    with _lock:
        _waiters.append(waiter)
    try:
        while version() == seen:
            left = deadline - loop.time()
            if left <= 0:
                return
            try:
                await asyncio.wait_for(ready.wait(), left)
            except asyncio.TimeoutError:
                return
            ready.clear()
        gap = REPORT_GAP - (time.time() - last_report)
        if gap > 0:
            await asyncio.sleep(min(gap, max(0.0, deadline - loop.time())))
    finally:
        with _lock:
            if waiter in _waiters:
                _waiters.remove(waiter)


# ── Noticing changes ─────────────────────────────────────────────────────────

def _changed(obj) -> bool:
    return any(attr.key not in QUIET_COLUMNS and attr.history.has_changes() for attr in inspect(obj).attrs)


def _rows(session: Session) -> Tuple[set, bool]:
    from core.db.models import ChatMessage, ChatSession
    from core.db.models.chat_runs import ChatRun

    chats, listed = set(), False
    for obj in session.new:
        if isinstance(obj, (ChatMessage, ChatRun)):
            chats.add(obj.chat_id)
        elif isinstance(obj, ChatSession):
            chats.add(obj.chat_id)
            listed = True
    for obj in session.deleted:
        if isinstance(obj, (ChatMessage, ChatRun, ChatSession)):
            chats.add(obj.chat_id)
            listed = listed or isinstance(obj, ChatSession)
    for obj in session.dirty:
        if isinstance(obj, (ChatMessage, ChatRun, ChatSession)) and _changed(obj):
            chats.add(obj.chat_id)
            listed = listed or isinstance(obj, ChatSession)
    return chats, listed


def _collect(session, _context):
    try:
        chats, listed = _rows(session)
    except Exception as exc:  # noqa: BLE001 - a missed report must never fail the write
        logger.warning("device_chat_changes_failed", error=type(exc).__name__)
        return
    if chats or listed:
        noted = session.info.setdefault("device_chats", [set(), False])
        noted[0].update(chats)
        noted[1] = noted[1] or listed


def _bulk(state):
    """Bulk statements bypass the session's lists of changed rows: find the chats they will touch first."""
    from core.db.models import ChatMessage, ChatSession
    from core.db.models.chat_runs import ChatRun

    if not (state.is_update or state.is_delete) or state.bind_mapper is None:
        return
    model = state.bind_mapper.class_
    where = getattr(state.statement, "whereclause", None)
    if model not in (ChatMessage, ChatSession, ChatRun) or where is None:
        return
    values = {getattr(key, "key", str(key)) for key in (getattr(state.statement, "_values", None) or {})}
    if state.is_update and values and values <= QUIET_COLUMNS:
        return
    try:
        chats = set(state.session.execute(select(model.chat_id).where(where)).scalars())
    except Exception as exc:  # noqa: BLE001 - a missed report must never fail the write
        logger.warning("device_chat_changes_failed", error=type(exc).__name__)
        return
    if chats:
        noted = state.session.info.setdefault("device_chats", [set(), False])
        noted[0].update(chats)
        # Deleting or renaming a chat changes the list.
        noted[1] = noted[1] or (model is ChatSession and (state.is_delete or bool(values & {"title", "deleted_at", "pinned"})))


def _committed(session):
    noted = session.info.pop("device_chats", None)
    if noted:
        _note(*noted)


def _rolled_back(session):
    session.info.pop("device_chats", None)


def install() -> None:
    """Start noticing this computer's chat changes; done once, by the first device link that starts."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    event.listen(Session, "after_flush", _collect)
    event.listen(Session, "do_orm_execute", _bulk)
    event.listen(Session, "after_commit", _committed)
    event.listen(Session, "after_rollback", _rolled_back)


def report_fields(sessions_enabled: bool) -> Tuple[dict, Optional[Tuple[list, bool]]]:
    """Heartbeat fields for the changes waiting, and what to restore if the report fails.

    Without session consent the cloud may not read these chats, so nothing is reported and
    nothing kept: consent granted later starts from a full read on the page anyway.
    """
    chats, listed = take()
    if not sessions_enabled or not (chats or listed):
        return {}, None
    fields = {"changed_chats": chats}
    if listed:
        fields["chat_list_changed"] = True
    return fields, (chats, listed)
