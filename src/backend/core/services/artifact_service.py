"""Artifact management business logic."""

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.content.artifact_refs import infer_artifact_type, resolve_artifact_storage_key
from core.db.models import Artifact as ArtifactModel
from core.db.repository import ArtifactRepository
from core.services.artifact_edition import artifact_scope_fields
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from core.services.project_scope import ProjectScope

logger = logging.getLogger(__name__)


def store_bytes_as_artifact(
    db: Session,
    *,
    user_id: str,
    content: bytes,
    filename: str,
    mime_type: Optional[str] = None,
    chat_id: Optional[str] = None,
    user_folder_id: Optional[str] = None,
    source: str = "user_upload",
    parsed_text: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ArtifactModel:
    """Persist bytes to storage + create one Artifact row; return the committed Artifact.

    User uploads (/v1/file/upload) and inbound channel attachments
    (core/channels/inbound) share this entry point, avoiding copy-pasted
    storage_key assembly + Artifact creation in two places. Leaving
    ``parsed_text`` empty means it will be lazily backfilled later.
    """
    env = os.getenv("ENVIRONMENT", "dev")
    artifact_id = f"ua_{uuid.uuid4().hex[:16]}"
    storage_key = f"{env}/{user_id}/user_uploads/{artifact_id}/{filename}"
    from core.storage import get_storage

    storage_url = get_storage().upload_bytes(content, storage_key)
    meta: Dict[str, Any] = {"source": source}
    if extra:
        meta.update(extra)
    artifact = ArtifactModel(
        artifact_id=artifact_id,
        chat_id=chat_id,
        user_id=user_id,
        user_folder_id=user_folder_id,
        type="other",
        title=filename,
        filename=filename,
        size_bytes=len(content),
        mime_type=mime_type or "application/octet-stream",
        storage_key=storage_key,
        storage_url=storage_url,
        parsed_text=parsed_text or None,
        extra_data=meta,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


class ArtifactService:
    """Service for artifact management."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = ArtifactRepository(db)

    def create_artifact(
        self,
        user_id: str,
        artifact_type: str,
        title: str,
        filename: str,
        size_bytes: int,
        mime_type: str,
        storage_key: str,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new artifact."""
        artifact_data = {
            "artifact_id": f"artifact_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "chat_id": chat_id,
            "type": artifact_type,
            "title": title,
            "filename": filename,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "storage_key": storage_key,
        }

        artifact = self.repo.create(artifact_data)

        return {
            "artifact_id": artifact.artifact_id,
            "type": artifact.type,
            "title": artifact.title,
            "filename": artifact.filename,
            "created_at": artifact.created_at.isoformat(),
        }

    def get_artifact(self, artifact_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Get artifact with ownership check."""
        artifact = self.repo.get_by_id(artifact_id)

        if not artifact or artifact.user_id != user_id:
            return None

        return {
            "artifact_id": artifact.artifact_id,
            "type": artifact.type,
            "title": artifact.title,
            "filename": artifact.filename,
            "storage_key": artifact.storage_key,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
            "created_at": artifact.created_at.isoformat(),
        }


# ── AI-generated artifact persistence (used by chat route + routing) ──────────
# Relocated from ``api/routes/v1/chats.py`` so lower layers (``core.llm.tool``,
# ``routing.*``) can persist artifacts without importing an API route module.


def persist_artifacts(
    db: Session,
    user_id: str,
    chat_id: str,
    collected: list,
    *,
    scope: Optional["ProjectScope"] = None,
) -> None:
    """Batch-insert AI-generated artifacts into the Artifact DB table.

    Strict mode: callers MUST pass only the list of files the agent
    explicitly pinned via ``pin_to_workspace`` (e.g. the result of
    ``core.llm.workspace.get_pinned()``). Intermediate tool outputs that
    the agent did not pin are deliberately NOT persisted — the user's
    "我的空间" should only show deliverables, not every transient docx
    produced by `word_replace_text` etc.

    User-uploaded files take a separate path (`api/routes/v1/file_upload.py`,
    extra_data.source = "user_upload") and never go through this helper.

    Project-mode auto-placement is delegated to the edition scope seam. The
    shared implementation only knows the personal folder field; commercial
    ownership columns are supplied by the enterprise module.

    **Important**: ``scope`` must be constructed explicitly by the caller from
    the workflow context and passed in. This function no longer reads the
    ContextVar — on the old path, by the time chats.py made the wrap-up call,
    workflow.py's finally had already reset the ContextVar, causing project
    outputs to land in the wrong scope.
    """
    if not collected:
        return

    scope_fields = artifact_scope_fields(scope)

    all_fids = [a["file_id"] for a in collected if a.get("file_id")]
    existing_ids = (
        set(
            r[0]
            for r in db.query(ArtifactModel.artifact_id)
            .filter(ArtifactModel.artifact_id.in_(all_fids))
            .all()
        )
        if all_fids
        else set()
    )
    for art in collected:
        art_id = art.get("file_id", "")
        if not art_id or art_id in existing_ids:
            continue
        mime = art.get("mime_type", "application/octet-stream")
        try:
            storage_key = (
                resolve_artifact_storage_key(art_id, art.get("storage_key"))
                or f"artifacts/{art_id}"
            )
            db.add(
                ArtifactModel(
                    artifact_id=art_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    type=infer_artifact_type(mime),
                    title=art.get("name", ""),
                    filename=art.get("name", ""),
                    size_bytes=max(art.get("size", 0) or 0, 1),
                    mime_type=mime,
                    storage_key=storage_key,
                    storage_url=art.get("url", ""),
                    extra_data={"source": "ai_generated", "tool_name": art.get("tool_name", "")},
                    **scope_fields,
                )
            )
        except Exception as e:
            logger.warning("artifact_db_insert_failed: %s", e)
    try:
        db.commit()
    except Exception as e:
        logger.warning("artifact_db_commit_failed: %s", e)
        db.rollback()


def extend_collected_artifacts(collected: list, refs: List[dict]) -> None:
    """Append new file refs to ``collected``, de-duplicating by file_id."""
    existing_ids = {item.get("file_id") for item in collected if item.get("file_id")}
    for ref in refs:
        file_id = ref.get("file_id")
        if not file_id or file_id in existing_ids:
            continue
        collected.append(ref)
        existing_ids.add(file_id)
