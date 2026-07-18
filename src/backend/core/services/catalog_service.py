"""Catalog override business logic."""

from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from core.db.repository import CatalogRepository, AuditLogRepository


class CatalogService:
    """Service for catalog management."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = CatalogRepository(db)

    def get_user_overrides(self, user_id: str, kind: Optional[str] = None) -> Dict[str, Any]:
        """Get all catalog overrides for a user, organized by kind."""
        overrides = self.repo.list_overrides(user_id, kind)

        result = {
            "skills": [],
            "agents": [],
            "mcps": []
        }

        for override in overrides:
            item = {
                "id": override.item_id,
                "enabled": override.enabled,
                "config": override.config_data
            }

            if override.kind == "skill":
                result["skills"].append(item)
            elif override.kind == "agent":
                result["agents"].append(item)
            elif override.kind == "mcp":
                result["mcps"].append(item)

        return result

    def update_override(
        self,
        user_id: str,
        kind: str,
        item_id: str,
        enabled: bool,
        config: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Update catalog override."""
        override = self.repo.upsert_override(user_id, kind, item_id, enabled, config)

        # Audit log
        audit_repo = AuditLogRepository(self.db)
        audit_repo.create({
            "user_id": user_id,
            "action": f"catalog.{kind}.{'enabled' if enabled else 'disabled'}",
            "resource_type": f"catalog_{kind}",
            "resource_id": item_id,
            "details": {"enabled": enabled, "config": config},
            "status": "success"
        })

        return {
            "kind": override.kind,
            "item_id": override.item_id,
            "enabled": override.enabled,
            "config": override.config_data
        }
