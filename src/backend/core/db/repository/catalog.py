"""Data access layer — catalog repositories.

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


class CatalogRepository:
    """Repository for catalog override operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_override(self, user_id: str, kind: str, item_id: str) -> Optional[CatalogOverride]:
        """Get catalog override for a specific item."""
        return self.db.query(CatalogOverride).filter(
            CatalogOverride.user_id == user_id,
            CatalogOverride.kind == kind,
            CatalogOverride.item_id == item_id
        ).first()

    def list_overrides(self, user_id: str, kind: Optional[str] = None) -> List[CatalogOverride]:
        """List all catalog overrides for a user."""
        query = self.db.query(CatalogOverride).filter(
            CatalogOverride.user_id == user_id
        )

        if kind:
            query = query.filter(CatalogOverride.kind == kind)

        return query.all()

    def upsert_override(self, user_id: str, kind: str, item_id: str, enabled: bool, config: Dict = None) -> CatalogOverride:
        """Create or update catalog override."""
        override = self.get_override(user_id, kind, item_id)

        if override:
            override.enabled = enabled
            if config is not None:
                override.config_data = config
            override.updated_at = datetime.utcnow()
        else:
            override = CatalogOverride(
                user_id=user_id,
                kind=kind,
                item_id=item_id,
                enabled=enabled,
                config_data=config or {}
            )
            self.db.add(override)

        self.db.commit()
        self.db.refresh(override)
        return override
