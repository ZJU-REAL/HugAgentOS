"""Repository template.

Replace ${Feature}, ${feature} with actual names.
Add to core/db/repository/<领域>.py.
"""

from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import desc
from core.db.models import ${Feature}


class ${Feature}Repository:
    """Repository for ${feature} operations."""

    def __init__(self, db: Session):
        self.db = db

    # --- Read ---

    def get_by_id(self, id: str) -> Optional[${Feature}]:
        """Get by primary key (respects soft delete)."""
        return self.db.query(${Feature}).filter(
            ${Feature}.id == id,
            ${Feature}.deleted_at.is_(None),
        ).first()

    def list_by_user(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[${Feature}], int]:
        """Paginated list for user."""
        query = self.db.query(${Feature}).filter(
            ${Feature}.user_id == user_id,
            ${Feature}.deleted_at.is_(None),
        )
        total = query.count()
        items = (
            query
            .order_by(desc(${Feature}.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    # --- Create ---

    def create(self, data: Dict[str, Any]) -> ${Feature}:
        """Create and commit."""
        item = ${Feature}(**data)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    # --- Update ---

    def update(self, id: str, data: Dict[str, Any]) -> Optional[${Feature}]:
        """Update fields and commit."""
        item = self.get_by_id(id)
        if not item:
            return None
        for key, value in data.items():
            setattr(item, key, value)
        item.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(item)
        return item

    # --- Delete (soft) ---

    def soft_delete(self, id: str) -> bool:
        """Mark as deleted."""
        item = self.get_by_id(id)
        if not item:
            return False
        item.deleted_at = datetime.utcnow()
        self.db.commit()
        return True
