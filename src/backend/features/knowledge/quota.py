"""Per-file cap and total quota for private (personal) knowledge bases."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.auth.capabilities import enforce_quota, enforce_upload_limit
from features.knowledge.models import KBDocument, KBSpace


def private_kb_usage_bytes(db: Session, user_id: str) -> int:
    """Bytes of live documents across the private knowledge bases ``user_id`` owns."""
    total = (
        db.query(sa.func.coalesce(sa.func.sum(KBDocument.size_bytes), 0))
        .join(KBSpace, KBSpace.kb_id == KBDocument.kb_id)
        .filter(
            KBSpace.user_id == user_id,
            KBSpace.visibility == "private",
            KBSpace.deleted_at.is_(None),
            KBDocument.deleted_at.is_(None),
        )
        .scalar()
    )
    return int(total or 0)


def enforce_private_kb_upload(db: Session, space: KBSpace, size_bytes: int) -> None:
    """413 when a document of ``size_bytes`` breaks the owner's private-KB file cap or quota."""
    if space is None or space.visibility != "private":
        return
    owner = str(space.user_id)
    enforce_upload_limit(db, owner, "personal_kb_upload_limit_mb", size_bytes, "个人知识库")
    enforce_quota(
        db,
        owner,
        "personal_kb_quota_mb",
        lambda: private_kb_usage_bytes(db, owner),
        size_bytes,
        "个人知识库",
    )
