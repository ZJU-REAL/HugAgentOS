"""Service layer template.

Replace ${Feature}, ${feature} with actual names.
Create as core/services/${feature}_service.py.
"""

import uuid
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session

from core.db.repository import ${Feature}Repository, AuditLogRepository
from core.db.models import ${Feature}
from core.infra.exceptions import ResourceOwnershipError


class ${Feature}Service:
    """Business logic for ${feature}s."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = ${Feature}Repository(db)
        self.audit_repo = AuditLogRepository(db)

    # --- Create ---

    def create(
        self,
        user_id: str,
        name: str,
        description: str = None,
        metadata: dict = None,
    ) -> ${Feature}:
        """Create a new ${feature}."""
        item = self.repo.create({
            "id": f"${feature}_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "name": name,
            "description": description,
            "extra_data": metadata or {},
        })

        self.audit_repo.create({
            "user_id": user_id,
            "action": "${feature}.created",
            "resource_type": "${feature}",
            "resource_id": item.id,
            "status": "success",
        })

        return item

    # --- Read ---

    def get_item(self, id: str, user_id: str) -> Optional[${Feature}]:
        """Get item with ownership check."""
        item = self.repo.get_by_id(id)
        if item and item.user_id != user_id:
            raise ResourceOwnershipError("${feature}", id)
        return item

    def list_items(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[${Feature}], int, int]:
        """List with pagination. Returns (items, total, total_pages)."""
        items, total = self.repo.list_by_user(user_id, page, page_size)
        total_pages = (total + page_size - 1) // page_size
        return items, total, total_pages

    # --- Update ---

    def update(
        self,
        id: str,
        user_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[${Feature}]:
        """Update with ownership check."""
        item = self.repo.get_by_id(id)
        if not item:
            return None
        if item.user_id != user_id:
            raise ResourceOwnershipError("${feature}", id)
        return self.repo.update(id, update_data)

    # --- Delete ---

    def delete(self, id: str, user_id: str) -> bool:
        """Soft delete with ownership check."""
        item = self.repo.get_by_id(id)
        if not item:
            return False
        if item.user_id != user_id:
            raise ResourceOwnershipError("${feature}", id)
        return self.repo.soft_delete(id)

    # --- Idempotent ensure ---

    def ensure(self, id: str, user_id: str, **defaults) -> Optional[${Feature}]:
        """Get existing or create (idempotent)."""
        existing = self.repo.get_by_id(id)
        if existing:
            if existing.user_id != user_id:
                return None
            return existing
        return self.create(user_id=user_id, **defaults)
