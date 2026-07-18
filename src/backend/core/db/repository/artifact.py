"""Data access layer — artifact repositories.

Split out of the former monolithic ``core/db/repository.py``. The package
``__init__`` re-exports every repository class, so ``from core.db.repository
import XxxRepository`` keeps working unchanged.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func, select
from core.db.models import (
    UserShadow, ChatSession, ChatMessage, CatalogOverride,
    KBSpace, KBDocument, Artifact, AuditLog, UserAgent,
    LocalUser, Team, TeamMember, TeamFolder, InviteCode,
)


#: Frontend counterpart constant: src/frontend/src/utils/constants.ts:ROOT_FOLDER_SENTINEL.
ROOT_FOLDER_SENTINEL = "__root__"


class ArtifactRepository:
    """Repository for artifact operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, artifact_id: str) -> Optional[Artifact]:
        """Get artifact by ID."""
        return self.db.query(Artifact).filter(
            Artifact.artifact_id == artifact_id,
            Artifact.deleted_at.is_(None)
        ).first()

    def list_by_user(
        self,
        user_id: str,
        artifact_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> tuple[List[Artifact], int]:
        """List artifacts for a user."""
        query = self.db.query(Artifact).filter(
            Artifact.user_id == user_id,
            Artifact.deleted_at.is_(None)
        )

        if artifact_type:
            query = query.filter(Artifact.type == artifact_type)

        total = query.count()
        artifacts = query.order_by(desc(Artifact.created_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return artifacts, total

    def list_by_user_with_chat(
        self,
        user_id: str,
        mime_prefix: Optional[str] = None,
        keyword: Optional[str] = None,
        source_kind: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        personal_only: bool = True,
        folder_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """List artifacts for a user with chat session title (JOIN).

        Args:
            mime_prefix: e.g. "image/" to filter images, or use negation via caller logic.
            keyword: fuzzy match on filename or title.
            source_kind: "user_upload" | "ai_generated"; filters on
                ``extra_data.source`` using a dialect-aware JSON accessor.
            personal_only: when True (default) return only personal files with team_id NULL.
            folder_id: only takes effect when personal_only=True.
                "__root__" → root directory only (user_folder_id IS NULL);
                "<id>"     → direct child files of that folder;
                None       → no folder filtering (legacy behavior, returns all personal files).
        """
        query = self.db.query(Artifact, ChatSession.title.label("chat_title")).outerjoin(
            ChatSession, Artifact.chat_id == ChatSession.chat_id
        ).filter(
            Artifact.user_id == user_id,
            Artifact.deleted_at.is_(None),
        )

        if personal_only:
            query = query.filter(Artifact.team_id.is_(None))
            if folder_id == ROOT_FOLDER_SENTINEL:
                query = query.filter(Artifact.user_folder_id.is_(None))
            elif folder_id:
                query = query.filter(Artifact.user_folder_id == folder_id)

        if mime_prefix == "image/":
            query = query.filter(Artifact.mime_type.like("image/%"))
        elif mime_prefix == "document":
            query = query.filter(~Artifact.mime_type.like("image/%"))

        if source_kind in ("user_upload", "ai_generated"):
            # Dialect-portable JSON path extraction on the Artifact.extra_data
            # (DB column name "metadata"): JSONB on PostgreSQL, JSON on SQLite.
            dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
            if dialect == "postgresql":
                json_source = func.jsonb_extract_path_text(Artifact.extra_data, "source")
            else:
                json_source = func.json_extract(Artifact.extra_data, "$.source")

            if source_kind == "user_upload":
                query = query.filter(json_source == "user_upload")
            else:
                # ai_generated = anything that is NOT explicitly user_upload,
                # including NULL / missing source metadata (e.g. backfill).
                query = query.filter(
                    or_(json_source.is_(None), json_source != "user_upload")
                )

        if keyword:
            like_pattern = f"%{keyword}%"
            query = query.filter(
                or_(
                    Artifact.filename.ilike(like_pattern),
                    Artifact.title.ilike(like_pattern),
                )
            )

        total = query.count()
        rows = query.order_by(desc(Artifact.created_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        items = []
        for artifact, chat_title in rows:
            items.append({
                "artifact": artifact,
                "chat_title": chat_title,
            })
        return items, total

    def soft_delete(self, artifact_id: str, user_id: str) -> bool:
        """Soft delete a personal MySpace artifact (set deleted_at)."""
        artifact = self.db.query(Artifact).filter(
            Artifact.artifact_id == artifact_id,
            Artifact.user_id == user_id,
            Artifact.deleted_at.is_(None),
        ).first()
        if not artifact:
            return False
        artifact.deleted_at = datetime.utcnow()
        self.db.commit()
        return True

    def create(self, artifact_data: Dict[str, Any]) -> Artifact:
        """Create a new artifact."""
        artifact = Artifact(**artifact_data)
        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)
        return artifact

    # ── Team-scoped queries ──────────────────────────────────────────

    def list_by_team_folder(
        self,
        team_id: str,
        folder_id: Optional[str],
        *,
        mime_prefix: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[List[Dict[str, Any]], int]:
        """List files under a team folder (folder_id=None means the team root)."""
        query = self.db.query(Artifact, ChatSession.title.label("chat_title")).outerjoin(
            ChatSession, Artifact.chat_id == ChatSession.chat_id
        ).filter(
            Artifact.team_id == team_id,
            Artifact.deleted_at.is_(None),
        )

        if folder_id is None:
            query = query.filter(Artifact.team_folder_id.is_(None))
        else:
            query = query.filter(Artifact.team_folder_id == folder_id)

        if mime_prefix == "image/":
            query = query.filter(Artifact.mime_type.like("image/%"))
        elif mime_prefix == "document":
            query = query.filter(~Artifact.mime_type.like("image/%"))

        if keyword:
            pattern = f"%{keyword}%"
            query = query.filter(
                or_(Artifact.filename.ilike(pattern), Artifact.title.ilike(pattern))
            )

        total = query.count()
        rows = (
            query.order_by(desc(Artifact.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items: List[Dict[str, Any]] = []
        for artifact, chat_title in rows:
            items.append({"artifact": artifact, "chat_title": chat_title})
        return items, total

    def move_artifact_to_team(
        self,
        artifact_id: str,
        *,
        team_id: str,
        folder_id: Optional[str],
        new_storage_key: Optional[str] = None,
        new_storage_url: Optional[str] = None,
        expected_uploader: Optional[str] = None,
    ) -> Optional[Artifact]:
        """Move a personal (or in-team) file into the specified team folder."""
        query = self.db.query(Artifact).filter(
            Artifact.artifact_id == artifact_id,
            Artifact.deleted_at.is_(None),
        )
        if expected_uploader is not None:
            query = query.filter(Artifact.user_id == expected_uploader)
        artifact = query.first()
        if artifact is None:
            return None
        artifact.team_id = team_id
        artifact.team_folder_id = folder_id
        if new_storage_key:
            artifact.storage_key = new_storage_key
        if new_storage_url is not None:
            artifact.storage_url = new_storage_url
        artifact.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(artifact)
        return artifact

    def soft_delete_team_file(
        self, artifact_id: str, *, team_id: str, actor_user_id: Optional[str] = None
    ) -> bool:
        """Soft-delete a team file; if actor_user_id is given, require it to have been uploaded by that user."""
        query = self.db.query(Artifact).filter(
            Artifact.artifact_id == artifact_id,
            Artifact.team_id == team_id,
            Artifact.deleted_at.is_(None),
        )
        if actor_user_id is not None:
            query = query.filter(Artifact.user_id == actor_user_id)
        artifact = query.first()
        if not artifact:
            return False
        artifact.deleted_at = datetime.utcnow()
        self.db.commit()
        return True
