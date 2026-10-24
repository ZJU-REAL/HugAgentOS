"""Repository template — data access only, no business rules.

Replace ${Feature}, ${feature} with actual names.
Feature module: features/${feature}/repository.py. Core domain: core/db/repository/<domain>.py.

Rules:
  - Synchronous; takes a Session. Called from sync routes or inside core.db.threads.run_in_session
  - Every read filters deleted_at IS NULL
  - Time via core.infra.time.utc_now
"""

from typing import Any, Dict, List, Optional, Tuple

from core.infra.time import utc_now
from features.${feature}.models import ${Feature}
from sqlalchemy import desc
from sqlalchemy.orm import Session


class ${Feature}Repository:
    def __init__(self, db: Session):
        self.db = db

    def _live(self):
        return self.db.query(${Feature}).filter(${Feature}.deleted_at.is_(None))

    def get_by_id(self, item_id: str) -> Optional[${Feature}]:
        return self._live().filter(${Feature}.id == item_id).first()

    def list_by_user(self, user_id: str, page: int = 1, page_size: int = 20) -> Tuple[List[${Feature}], int]:
        query = self._live().filter(${Feature}.user_id == user_id)
        total = query.count()
        items = (
            query.order_by(desc(${Feature}.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def create(self, data: Dict[str, Any]) -> ${Feature}:
        item = ${Feature}(**data)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, item: ${Feature}, data: Dict[str, Any]) -> ${Feature}:
        for key, value in data.items():
            setattr(item, key, value)
        self.db.commit()
        self.db.refresh(item)
        return item

    def soft_delete(self, item: ${Feature}) -> None:
        item.deleted_at = utc_now()
        self.db.commit()
