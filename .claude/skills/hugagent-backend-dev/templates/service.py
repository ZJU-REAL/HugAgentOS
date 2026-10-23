"""Service template — business rules and permission checks.

Replace ${Feature}, ${feature} with actual names.
Feature module: features/${feature}/service.py. Core domain: core/<domain>/<module>.py
(there is no core/services/ package any more — services live with the domain that owns them).

Rules:
  - Synchronous; constructor takes a Session and builds its repositories
  - Raises core.infra.exceptions.* (never HTTPException, never returns error dicts)
  - Ownership / permission checks live here, not in the route
  - No network I/O inside a method that holds the Session: do DB work, return, await network
    in the async caller, then open a new DB step (core.db.threads)
  - Module ≤ 500 lines; split by responsibility before it grows past that
"""

import uuid
from typing import List, Optional, Tuple

from core.infra.exceptions import BadRequestError, ResourceNotFoundError, ResourceOwnershipError
from features.${feature}.models import ${Feature}
from features.${feature}.repository import ${Feature}Repository
from sqlalchemy.orm import Session


class ${Feature}Service:
    def __init__(self, db: Session):
        self.db = db
        self.repo = ${Feature}Repository(db)

    def get_owned(self, item_id: str, user_id: str) -> ${Feature}:
        item = self.repo.get_by_id(item_id)
        if item is None:
            raise ResourceNotFoundError("${feature}", item_id)
        if item.user_id != user_id:
            raise ResourceOwnershipError("${feature}", item_id)
        return item

    def list_items(self, user_id: str, page: int, page_size: int) -> Tuple[List[${Feature}], int]:
        return self.repo.list_by_user(user_id, page, page_size)

    def create(self, user_id: str, name: str, description: Optional[str] = None) -> ${Feature}:
        name = name.strip()
        if not name:
            raise BadRequestError("名称不能为空")
        return self.repo.create(
            {
                "id": f"${feature}_{uuid.uuid4().hex[:16]}",
                "user_id": user_id,
                "name": name,
                "description": description,
            }
        )

    def update(self, item_id: str, user_id: str, changes: dict) -> ${Feature}:
        item = self.get_owned(item_id, user_id)
        allowed = {k: v for k, v in changes.items() if k in {"name", "description", "is_enabled"} and v is not None}
        return self.repo.update(item, allowed)

    def delete(self, item_id: str, user_id: str) -> None:
        self.repo.soft_delete(self.get_owned(item_id, user_id))
