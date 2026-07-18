"""Data access layer — knowledge base repositories.

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
    KBGrant,
)


class KBRepository:
    """Repository for knowledge base operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_space(self, kb_id: str) -> Optional[KBSpace]:
        """Get KB space by ID."""
        return self.db.query(KBSpace).filter(
            KBSpace.kb_id == kb_id,
            KBSpace.deleted_at.is_(None)
        ).first()

    def list_spaces(self, user_id: str) -> List[KBSpace]:
        """List all KB spaces for a user."""
        return self.db.query(KBSpace).filter(
            KBSpace.user_id == user_id,
            KBSpace.deleted_at.is_(None)
        ).all()

    def list_public_spaces(self) -> List[KBSpace]:
        """List all public KB spaces (visibility == 'public'), regardless of owner.

        Public spaces are admin-managed shared knowledge bases that every user can
        see in the catalog and retrieve from. Ordered by creation time (oldest first).
        """
        return self.db.query(KBSpace).filter(
            KBSpace.visibility == "public",
            KBSpace.deleted_at.is_(None)
        ).order_by(KBSpace.created_at).all()

    def get_public_space(self, kb_id: str) -> Optional[KBSpace]:
        """Get a public KB space by ID (visibility == 'public', not deleted)."""
        return self.db.query(KBSpace).filter(
            KBSpace.kb_id == kb_id,
            KBSpace.visibility == "public",
            KBSpace.deleted_at.is_(None)
        ).first()

    def list_shared_spaces(self) -> List[KBSpace]:
        """List admin-managed shared KB spaces (visibility public or scoped).

        The admin console needs to manage both "public to everyone" and "designated-visibility" shared bases, so scoped is included.
        """
        return self.db.query(KBSpace).filter(
            KBSpace.visibility.in_(("public", "scoped")),
            KBSpace.deleted_at.is_(None)
        ).order_by(KBSpace.created_at).all()

    def get_shared_space(self, kb_id: str) -> Optional[KBSpace]:
        """Get a shared KB space (visibility public or scoped, not deleted)."""
        return self.db.query(KBSpace).filter(
            KBSpace.kb_id == kb_id,
            KBSpace.visibility.in_(("public", "scoped")),
            KBSpace.deleted_at.is_(None)
        ).first()

    def create_space(self, space_data: Dict[str, Any]) -> KBSpace:
        """Create a new KB space."""
        space = KBSpace(**space_data)
        self.db.add(space)
        self.db.commit()
        self.db.refresh(space)
        return space

    def update_space(self, kb_id: str, update_data: Dict[str, Any]) -> Optional[KBSpace]:
        """Update a KB space."""
        space = self.get_space(kb_id)
        if not space:
            return None

        for key, value in update_data.items():
            setattr(space, key, value)

        space.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(space)
        return space

    def get_document(self, document_id: str) -> Optional[KBDocument]:
        """Get KB document by ID."""
        return self.db.query(KBDocument).filter(
            KBDocument.document_id == document_id,
            KBDocument.deleted_at.is_(None)
        ).first()

    def list_documents(
        self,
        kb_id: str,
        page: int = 1,
        page_size: int = 20
    ) -> tuple[List[KBDocument], int]:
        """List documents in a KB space."""
        query = self.db.query(KBDocument).filter(
            KBDocument.kb_id == kb_id,
            KBDocument.deleted_at.is_(None)
        )

        total = query.count()
        documents = query.order_by(desc(KBDocument.uploaded_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return documents, total

    def create_document(self, document_data: Dict[str, Any]) -> KBDocument:
        """Create a new KB document."""
        document = KBDocument(**document_data)
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document


class KBGrantRepository:
    """Data-access layer for knowledge-base grants + external-base visibility."""

    def __init__(self, db: Session):
        self.db = db

    # ── grants (common to local bases / Dify datasets) ──────────────────────────────
    def list_for_principal(self, principal_type: str, principal_id: str) -> List[KBGrant]:
        """Which resources a given user/team has been granted."""
        return self.db.query(KBGrant).filter(
            KBGrant.principal_type == principal_type,
            KBGrant.principal_id == principal_id,
        ).all()

    def upsert(
        self,
        resource_id: str,
        resource_type: str,
        principal_type: str,
        principal_id: str,
        level: str,
        granted_by: Optional[str] = None,
    ) -> KBGrant:
        grant = self.db.query(KBGrant).filter(
            KBGrant.resource_id == resource_id,
            KBGrant.resource_type == resource_type,
            KBGrant.principal_type == principal_type,
            KBGrant.principal_id == principal_id,
        ).first()
        if grant:
            grant.level = level
            if granted_by:
                grant.granted_by = granted_by
        else:
            grant = KBGrant(
                resource_id=resource_id,
                resource_type=resource_type,
                principal_type=principal_type,
                principal_id=principal_id,
                level=level,
                granted_by=granted_by,
            )
            self.db.add(grant)
        self.db.commit()
        self.db.refresh(grant)
        return grant

    def replace_for_principal(
        self,
        principal_type: str,
        principal_id: str,
        grants: List[Dict[str, str]],
        granted_by: Optional[str] = None,
    ) -> int:
        """Fully replace all grants of a given principal ("save" semantics of the user/team management page).

        Each ``grants`` item is like ``{resource_id, resource_type, level}``. Returns the number of rows written.
        """
        self.db.query(KBGrant).filter(
            KBGrant.principal_type == principal_type,
            KBGrant.principal_id == principal_id,
        ).delete()
        count = 0
        for g in grants:
            resource_id = str(g.get("resource_id") or "").strip()
            resource_type = str(g.get("resource_type") or "local").strip()
            level = str(g.get("level") or "view").strip()
            if not resource_id or resource_type not in ("local", "dify") or level not in ("view", "edit", "admin"):
                continue
            self.db.add(KBGrant(
                resource_id=resource_id,
                resource_type=resource_type,
                principal_type=principal_type,
                principal_id=principal_id,
                level=level,
                granted_by=granted_by,
            ))
            count += 1
        self.db.commit()
        return count

    def bulk_grant(
        self,
        resource_id: str,
        resource_type: str,
        principals: List[tuple[str, str]],
        level: str,
        granted_by: Optional[str] = None,
    ) -> int:
        """Add multiple grants to a resource at once (only for a just-created base, whose rows are guaranteed brand new). One commit.

        Each ``principals`` item is ``(principal_type, principal_id)``. Returns the number of rows written.
        """
        for principal_type, principal_id in principals:
            self.db.add(KBGrant(
                resource_id=resource_id,
                resource_type=resource_type,
                principal_type=principal_type,
                principal_id=principal_id,
                level=level,
                granted_by=granted_by,
            ))
        self.db.commit()
        return len(principals)


#: folder_id sentinel for list_by_user_with_chat: root directory only (user_folder_id IS NULL).
