"""Remove a deleted chat's upload snapshots once no live chat still shows them.

Every upload is stored twice: the artifact is the chat's immutable snapshot and My Space holds
the editable working copy. The snapshot belongs to the conversation, so deleting the chat frees
it — unless a fork (forks copy message attachments by ``file_id``, also across users) is alive.
The My Space copy is never touched here.
"""
from __future__ import annotations

import logging

from sqlalchemy import String, cast, false, or_

from core.db.models import Artifact, ChatMessage, ChatSession
from core.infra.time import utc_now

logger = logging.getLogger(__name__)


def _attachment_ids(extra_data) -> set[str]:
    return {item.get("file_id") for item in (extra_data or {}).get("attachments") or []
            if isinstance(item, dict) and item.get("file_id")}


def _fork_family(db, root: str) -> set[str]:
    """``root`` plus every chat forked from it, transitively (deleted ones included)."""
    family, frontier = {root}, {root}
    while frontier:
        rows = db.query(ChatSession.chat_id, ChatSession.extra_data).filter(
            or_(*(cast(ChatSession.extra_data, String).like(f"%{chat_id}%") for chat_id in frontier)),
        ).all()
        frontier = {
            chat_id for chat_id, extra in rows
            if ((extra or {}).get("fork") or {}).get("source_chat_id") in frontier and chat_id not in family
        }
        family |= frontier
    return family


def _still_shown(db, artifact_id: str, family: set[str]) -> bool:
    rows = db.query(ChatMessage.extra_data).join(
        ChatSession, ChatSession.chat_id == ChatMessage.chat_id,
    ).filter(
        ChatSession.chat_id.in_(family), ChatSession.deleted_at.is_(None),
        cast(ChatMessage.extra_data, String).like(f"%{artifact_id}%"),
    ).all()
    return any(artifact_id in _attachment_ids(extra) for (extra,) in rows)


def release_chat_snapshots(db, chat_id: str, storage) -> list[str]:
    """Soft-delete and free the upload snapshots of ``chat_id`` that no live chat still shows.

    Covers the chat's own uploads and, when the chat is a fork, the originals it shows whose
    owning chat is already gone. Returns the released artifact ids.
    """
    shown = set()
    for (extra,) in db.query(ChatMessage.extra_data).filter(ChatMessage.chat_id == chat_id).all():
        shown |= _attachment_ids(extra)
    candidates = db.query(Artifact).filter(
        Artifact.deleted_at.is_(None),
        or_(Artifact.chat_id == chat_id, Artifact.artifact_id.in_(shown) if shown else false()),
    ).all()
    released, families = [], {}
    for artifact in candidates:
        if (artifact.extra_data or {}).get("source") != "user_upload" or not artifact.chat_id:
            continue
        root = artifact.chat_id
        if root not in families:
            families[root] = _fork_family(db, root)
        if _still_shown(db, artifact.artifact_id, families[root]):
            continue
        artifact.deleted_at = utc_now()
        released.append((artifact.artifact_id, artifact.storage_key))
    db.commit()
    for artifact_id, key in released:
        try:
            storage.delete(key)
        except Exception:  # the row is already gone from every listing; a leftover object is harmless
            logger.warning("Failed to delete snapshot object for %s", artifact_id, exc_info=True)
    return [artifact_id for artifact_id, _ in released]
