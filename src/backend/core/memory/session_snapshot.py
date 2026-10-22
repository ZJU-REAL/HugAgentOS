"""Persistence for the session-frozen memory block.

The frozen block is a *session-level* artifact: it is assembled on the first
turn that has memory enabled and then replayed verbatim for the rest of the
chat. It is injected at the head of the message list, ahead of the whole
conversation, so rebuilding it per turn moves the prefix-cache boundary to the
very front and forces the model to re-prefill the entire context on every turn.
Measured on a 325k-token chat: 54s to first token on each turn's first model
call, against 17s for the second call in the same turn, which kept the prefix.

Stored on ``ChatSession.extra_data`` next to the other chat-sticky runtime
state (see ``core.llm.runtime.session_capabilities``). The snapshot records the memory
scope it was built for, because a snapshot assembled for one scope must never
be served to another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_SNAPSHOT_KEY = "memory_frozen_snapshot"


@dataclass(frozen=True)
class SessionMemorySnapshot:
    """The frozen block together with the memory scope that produced it."""

    text: str
    scope_user_id: str
    workspace_id: str

    def matches(self, *, scope_user_id: str, workspace_id: str) -> bool:
        return self.scope_user_id == scope_user_id and self.workspace_id == workspace_id

    def to_payload(self) -> dict:
        return {
            "text": self.text,
            "scope_user_id": self.scope_user_id,
            "workspace_id": self.workspace_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Optional["SessionMemorySnapshot"]:
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        scope_user_id = payload.get("scope_user_id")
        workspace_id = payload.get("workspace_id")
        if not isinstance(text, str) or not isinstance(scope_user_id, str):
            return None
        if not isinstance(workspace_id, str):
            return None
        return cls(text=text, scope_user_id=scope_user_id, workspace_id=workspace_id)


def load_session_memory_snapshot(
    chat_id: Optional[str],
    *,
    scope_user_id: str,
    workspace_id: str,
) -> Optional[SessionMemorySnapshot]:
    """Return this chat's frozen block, or ``None`` when it has yet to be built.

    An empty ``text`` is a real answer — "this session starts with nothing known
    about the user" — and is returned as a snapshot, not as ``None``. Only a
    missing record makes the caller assemble one.
    """
    if not chat_id:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

        with SessionLocal() as db:
            row = db.query(ChatSession.extra_data).filter(ChatSession.chat_id == chat_id).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] frozen snapshot load failed chat=%s: %s", chat_id, exc)
        return None

    data = (row[0] if row else None) or {}
    snapshot = SessionMemorySnapshot.from_payload(data.get(_SNAPSHOT_KEY))
    if snapshot is None:
        return None
    if not snapshot.matches(scope_user_id=scope_user_id, workspace_id=workspace_id):
        logger.info(
            "[memory] frozen snapshot scope changed chat=%s stored=%s/%s current=%s/%s",
            chat_id,
            snapshot.scope_user_id,
            snapshot.workspace_id,
            scope_user_id,
            workspace_id,
        )
        return None
    return snapshot


def save_session_memory_snapshot(
    chat_id: Optional[str],
    snapshot: SessionMemorySnapshot,
) -> SessionMemorySnapshot:
    """Record the frozen block for this chat, returning whatever the chat ends up frozen on.

    First writer wins. Two runs opening the same chat at the same moment both find no
    snapshot and both retrieve; without this the second would overwrite the first and the
    session would disagree with itself about what it froze. The row is locked for the
    read-modify-write so the two cannot interleave, and a caller that loses the race gets
    the stored snapshot back and injects that instead of its own.
    """
    if not chat_id:
        return snapshot
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession
        from sqlalchemy.orm.attributes import flag_modified

        with SessionLocal() as db:
            row = (
                db.query(ChatSession)
                .filter(ChatSession.chat_id == chat_id)
                .with_for_update()
                .first()
            )
            if row is None:
                return snapshot
            data = dict(row.extra_data or {})
            stored = SessionMemorySnapshot.from_payload(data.get(_SNAPSHOT_KEY))
            if stored is not None and stored.matches(
                scope_user_id=snapshot.scope_user_id, workspace_id=snapshot.workspace_id
            ):
                return stored
            data[_SNAPSHOT_KEY] = snapshot.to_payload()
            row.extra_data = data
            flag_modified(row, "extra_data")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] frozen snapshot persist failed chat=%s: %s", chat_id, exc)
    return snapshot


__all__ = [
    "SessionMemorySnapshot",
    "load_session_memory_snapshot",
    "save_session_memory_snapshot",
]
