"""Repository — site hosting (sites / site_kv / site_submissions tables)."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from core.db.models import Site, SiteKV, SiteSubmission


class SiteRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, site_id: str) -> Optional[Site]:
        return (
            self.db.query(Site)
            .filter(Site.site_id == site_id, Site.deleted_at.is_(None))
            .first()
        )

    def get_by_slug(self, slug: str) -> Optional[Site]:
        return (
            self.db.query(Site)
            .filter(Site.slug == slug, Site.deleted_at.is_(None))
            .first()
        )

    def list_by_user(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> Tuple[List[Site], int]:
        query = self.db.query(Site).filter(
            Site.user_id == user_id, Site.deleted_at.is_(None)
        )
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
        item.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(item)
        return item

    def soft_delete(self, site_id: str) -> bool:
        """Soft-delete and release the slug (rewritten to ``<slug>--del-<ts>`` so the original address can be reused)."""
        item = self.get_by_id(site_id)
        if not item:
            return False
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        item.slug = f"{item.slug}--del-{ts}"[:80]
        item.deleted_at = datetime.utcnow()
        self.db.commit()
        return True

    def increment_view(self, site_id: str) -> None:
        """Atomic +1 (no refresh, to avoid an extra query on the hosting hot path)."""
        self.db.query(Site).filter(Site.site_id == site_id).update(
            {Site.view_count: Site.view_count + 1}, synchronize_session=False
        )
        self.db.commit()

    # ── KV ───────────────────────────────────────────────────────

    def kv_get(self, site_id: str, key: str) -> Optional[SiteKV]:
        return (
            self.db.query(SiteKV)
            .filter(SiteKV.site_id == site_id, SiteKV.k == key)
            .first()
        )

    def kv_set(self, site_id: str, key: str, value: str) -> SiteKV:
        row = self.kv_get(site_id, key)
        if row:
            row.v = value
            row.updated_at = datetime.utcnow()
        else:
            row = SiteKV(site_id=site_id, k=key, v=value)
            self.db.add(row)
        self.db.commit()
        return row

    def kv_delete(self, site_id: str, key: str) -> bool:
        n = (
            self.db.query(SiteKV)
            .filter(SiteKV.site_id == site_id, SiteKV.k == key)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return n > 0

    def kv_list(self, site_id: str, limit: int = 500) -> List[SiteKV]:
        return (
            self.db.query(SiteKV)
            .filter(SiteKV.site_id == site_id)
            .order_by(SiteKV.k)
            .limit(limit)
            .all()
        )

    def kv_count(self, site_id: str) -> int:
        return (
            self.db.query(func.count())
            .select_from(SiteKV)
            .filter(SiteKV.site_id == site_id)
            .scalar()
            or 0
        )

    def kv_clear(self, site_id: str) -> int:
        n = (
            self.db.query(SiteKV)
            .filter(SiteKV.site_id == site_id)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return n

    # ── Form collection ─────────────────────────────────────────────────

    def submission_add(
        self, site_id: str, form_key: str, payload: Dict[str, Any],
        client_ip: Optional[str] = None,
    ) -> SiteSubmission:
        row = SiteSubmission(
            id=f"subm_{uuid.uuid4().hex[:16]}",
            site_id=site_id,
            form_key=form_key,
            payload=payload,
            client_ip=client_ip,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def submission_list(
        self, site_id: str, page: int = 1, page_size: int = 50,
        form_key: Optional[str] = None,
    ) -> Tuple[List[SiteSubmission], int]:
        query = self.db.query(SiteSubmission).filter(SiteSubmission.site_id == site_id)
        if form_key:
            query = query.filter(SiteSubmission.form_key == form_key)
        total = query.count()
        items = (
            query.order_by(desc(SiteSubmission.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def submission_count(self, site_id: str) -> int:
        return (
            self.db.query(func.count())
            .select_from(SiteSubmission)
            .filter(SiteSubmission.site_id == site_id)
            .scalar()
            or 0
        )

    def submission_clear(self, site_id: str) -> int:
        n = (
            self.db.query(SiteSubmission)
            .filter(SiteSubmission.site_id == site_id)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return n
