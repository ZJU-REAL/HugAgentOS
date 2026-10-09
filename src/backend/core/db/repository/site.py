"""Repository — site hosting (sites table)."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import Site
from core.infra.time import utc_now
from sqlalchemy import desc
from sqlalchemy.orm import Session


class SiteRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, site_id: str) -> Optional[Site]:
        return (
            self.db.query(Site).filter(Site.site_id == site_id, Site.deleted_at.is_(None)).first()
        )

    def get_by_slug(self, slug: str) -> Optional[Site]:
        return self.db.query(Site).filter(Site.slug == slug, Site.deleted_at.is_(None)).first()

    def list_by_user(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> Tuple[List[Site], int]:
        query = self.db.query(Site).filter(Site.user_id == user_id, Site.deleted_at.is_(None))
        total = query.count()
        items = (
            query.order_by(desc(Site.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def create(self, data: Dict[str, Any]) -> Site:
        item = Site(**data)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, site_id: str, data: Dict[str, Any]) -> Optional[Site]:
        item = self.get_by_id(site_id)
        if not item:
            return None
        for key, value in data.items():
            setattr(item, key, value)
        item.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(item)
        return item

    def soft_delete(self, site_id: str) -> bool:
        """Soft-delete and release the slug (rewritten to ``<slug>--del-<ts>`` so the original address can be reused)."""
        item = self.get_by_id(site_id)
        if not item:
            return False
        ts = utc_now().strftime("%Y%m%d%H%M%S")
        item.slug = f"{item.slug}--del-{ts}"[:80]
        item.deleted_at = utc_now()
        self.db.commit()
        return True

    def increment_view(self, site_id: str) -> None:
        """Atomic +1 (no refresh, to avoid an extra query on the hosting hot path)."""
        self.db.query(Site).filter(Site.site_id == site_id).update(
            {Site.view_count: Site.view_count + 1}, synchronize_session=False
        )
        self.db.commit()
