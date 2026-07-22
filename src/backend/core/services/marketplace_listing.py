"""Community marketplace listing state.

CE keeps the global list/delist switch. Organization-scoped visibility and
principal grants are intentionally absent: every enabled item is public.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.db.models import MarketplaceListingState
from sqlalchemy.orm import Session

KIND_PLUGIN = "plugin"
KIND_SKILL = "skill"
KIND_AGENT = "agent"


def get_disabled_ids(db: Session, kind: str) -> set[str]:
    return {
        row[0]
        for row in db.query(MarketplaceListingState.item_id)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.enabled.is_(False),
        )
        .all()
    }


def set_listing_enabled(
    db: Session,
    kind: str,
    item_id: str,
    enabled: bool,
    *,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    row = (
        db.query(MarketplaceListingState)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.item_id == item_id,
        )
        .first()
    )
    if row is None:
        row = MarketplaceListingState(kind=kind, item_id=item_id)
        db.add(row)
    row.enabled = bool(enabled)
    row.updated_at = datetime.utcnow()
    row.updated_by = updated_by
    db.commit()
    return {"kind": kind, "item_id": item_id, "enabled": bool(enabled)}


def annotate_and_filter(
    db: Session,
    kind: str,
    items: List[Dict[str, Any]],
    *,
    id_key: str,
    include_disabled: bool,
    viewer_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    disabled = get_disabled_ids(db, kind)
    out: List[Dict[str, Any]] = []
    for item in items:
        enabled = str(item.get(id_key)) not in disabled
        if not include_disabled and not enabled:
            continue
        item["market_enabled"] = enabled
        out.append(item)
    return out


def ensure_item_visible(
    db: Session,
    kind: str,
    item_id: str,
    user_id: Optional[str],
    *,
    resource: str,
) -> None:
    """All enabled CE marketplace items are visible."""


__all__ = [
    "KIND_AGENT",
    "KIND_PLUGIN",
    "KIND_SKILL",
    "annotate_and_filter",
    "ensure_item_visible",
    "get_disabled_ids",
    "set_listing_enabled",
]
