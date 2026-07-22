"""Single-tenant knowledge-base authorization for the community edition."""

from __future__ import annotations

from typing import Dict, List, Literal, Set

from sqlalchemy.orm import Session

KBLevel = Literal["none", "view", "edit", "admin"]
_RANK = {"none": 0, "view": 1, "edit": 2, "admin": 3}


def has_kb_permission(current: str, required: str) -> bool:
    return _RANK.get(current, 0) >= _RANK.get(required, 0)


def is_shared_visibility(visibility: str) -> bool:
    return visibility != "private"


def level_to_caps(level: str) -> Dict[str, bool]:
    return {
        "can_view": has_kb_permission(level, "view"),
        "can_edit": has_kb_permission(level, "edit"),
        "can_admin": has_kb_permission(level, "admin"),
    }


def _is_super_admin(db: Session, user_id: str) -> bool:
    from core.db.models import UserShadow

    row = db.query(UserShadow.extra_data).filter(UserShadow.user_id == user_id).first()
    metadata = row[0] if row and isinstance(row[0], dict) else {}
    return metadata.get("role") == "super_admin"


def get_accessible_local_kb_levels(db: Session, user_id: str) -> Dict[str, KBLevel]:
    from core.db.models import KBSpace

    if not user_id:
        return {}
    is_admin = _is_super_admin(db, user_id)
    rows = (
        db.query(KBSpace.kb_id, KBSpace.visibility, KBSpace.user_id)
        .filter(KBSpace.deleted_at.is_(None))
        .all()
    )
    return {
        kb_id: "admin" if is_admin or owner == user_id else "view"
        for kb_id, visibility, owner in rows
        if is_admin or owner == user_id or is_shared_visibility(visibility)
    }


def get_accessible_local_kb_ids(db: Session, user_id: str) -> Set[str]:
    return set(get_accessible_local_kb_levels(db, user_id))


def get_dataset_levels(db: Session, user_id: str, dataset_ids: List[str]) -> Dict:
    return {}


def filter_accessible_kb_ids(db: Session, user_id: str, kb_ids: List[str]) -> List[str]:
    allowed = get_accessible_local_kb_ids(db, user_id)
    return [kb_id for kb_id in kb_ids if kb_id in allowed]


def resolve_local_kb_level(db: Session, user_id: str, kb_id: str) -> KBLevel:
    return get_accessible_local_kb_levels(db, user_id).get(kb_id, "none")


__all__ = [
    "KBLevel",
    "filter_accessible_kb_ids",
    "get_accessible_local_kb_ids",
    "get_accessible_local_kb_levels",
    "get_dataset_levels",
    "has_kb_permission",
    "is_shared_visibility",
    "level_to_caps",
    "resolve_local_kb_level",
]
