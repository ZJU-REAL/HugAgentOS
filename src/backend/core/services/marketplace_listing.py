"""Marketplace listing state: shared by the plugin marketplace / skill marketplace.

Controls whether a given plugin/skill is shown in the marketplace. **A missing row means
enabled** (all items listed by default, not cleared on upgrade); only when an admin
explicitly disables one is a row with ``enabled=false`` written. The user-facing
marketplace shows only enabled items; the admin backend shows all, annotated with
``market_enabled``. Physical deletion (uploaded items only) goes through their respective
delete endpoints; this module only handles visibility.

Visibility scope: ``public`` (default, same as a missing row) visible to everyone;
``scoped`` visible only to authorized users/teams/roles (the allowlist is stored in
``marketplace_visibility_grants``, resolution see core/auth/marketplace_visibility.py).
The user-facing list/detail/install filter by this; the admin backend does not filter,
annotating with ``visibility`` plus a grant-config endpoint. Only handles marketplace
browsing and installation, does not trace back already-installed instances.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.db.models import MarketplaceListingState, MarketplaceVisibilityGrant
from core.infra.exceptions import BadRequestError, ResourceNotFoundError

KIND_PLUGIN = "plugin"
KIND_SKILL = "skill"
KIND_AGENT = "agent"

VISIBILITY_PUBLIC = "public"
VISIBILITY_SCOPED = "scoped"
_VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_SCOPED)
_PRINCIPAL_TYPES = ("user", "team", "role")


def get_disabled_ids(db: Session, kind: str) -> set:
    """Set of item_ids disabled by an admin in this marketplace (missing row=enabled, so only query enabled=false)."""
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
    db: Session, kind: str, item_id: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """List/delist a marketplace item (idempotent upsert)."""
    row = (
        db.query(MarketplaceListingState)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.item_id == item_id,
        )
        .first()
    )
    now = datetime.utcnow()
    if row is None:
        row = MarketplaceListingState(kind=kind, item_id=item_id)
        db.add(row)
    row.enabled = bool(enabled)
    row.updated_at = now
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
    """Annotate each item with ``market_enabled`` / ``visibility``; the user side
    (include_disabled=False) filters out disabled items, and by visibility scope filters
    out scoped items the current user (``viewer_user_id``) has no right to see.
    The admin backend (include_disabled=True) sees all, relying on ``visibility`` to show scope state.
    """
    disabled = get_disabled_ids(db, kind)
    scoped = get_scoped_ids(db, kind)
    hidden: set = set()
    if not include_disabled:
        from core.auth.marketplace_visibility import get_hidden_item_ids
        hidden = get_hidden_item_ids(db, kind, viewer_user_id)
    out: List[Dict[str, Any]] = []
    for it in items:
        item_id = str(it.get(id_key))
        is_on = item_id not in disabled
        if not include_disabled and (not is_on or item_id in hidden):
            continue
        it["market_enabled"] = is_on
        it["visibility"] = VISIBILITY_SCOPED if item_id in scoped else VISIBILITY_PUBLIC
        out.append(it)
    return out


# ── Visibility scope (visibility + grants) ──────────────────────────────────────────

def get_scoped_ids(db: Session, kind: str) -> set:
    """Set of item_ids in this marketplace set to scoped (visible to a specified scope) (missing row=public, so only query scoped)."""
    return {
        row[0]
        for row in db.query(MarketplaceListingState.item_id)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.visibility == VISIBILITY_SCOPED,
        )
        .all()
    }


def get_listing_visibility(db: Session, kind: str, item_id: str) -> Dict[str, Any]:
    """A single item's visibility scope config: {visibility, grants:[{principal_type, principal_id}]}."""
    row = (
        db.query(MarketplaceListingState.visibility)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.item_id == item_id,
        )
        .first()
    )
    visibility = row[0] if row and row[0] in _VISIBILITIES else VISIBILITY_PUBLIC
    grants = [
        {"principal_type": g.principal_type, "principal_id": g.principal_id}
        for g in db.query(MarketplaceVisibilityGrant)
        .filter(
            MarketplaceVisibilityGrant.kind == kind,
            MarketplaceVisibilityGrant.item_id == item_id,
        )
        .order_by(
            MarketplaceVisibilityGrant.principal_type,
            MarketplaceVisibilityGrant.principal_id,
        )
        .all()
    ]
    return {"kind": kind, "item_id": item_id, "visibility": visibility, "grants": grants}


def set_listing_visibility(
    db: Session,
    kind: str,
    item_id: str,
    *,
    visibility: str,
    grants: Optional[List[Dict[str, str]]] = None,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Set an item's visibility scope (idempotent upsert + full replacement of grants).

    ``public`` → clear grants; ``scoped`` → replace the entire allowlist with ``grants``
    (at least one, otherwise the item is invisible to all regular users, which is a
    config error, straight 400).
    """
    if visibility not in _VISIBILITIES:
        raise BadRequestError(message=f"visibility 仅支持 {', '.join(_VISIBILITIES)}")
    grants = grants or []
    seen: set = set()
    normalized: List[Dict[str, str]] = []
    for g in grants:
        ptype = str(g.get("principal_type") or "").strip()
        pid = str(g.get("principal_id") or "").strip()
        if ptype not in _PRINCIPAL_TYPES:
            raise BadRequestError(message=f"principal_type 仅支持 {', '.join(_PRINCIPAL_TYPES)}")
        if not pid:
            raise BadRequestError(message="principal_id 不能为空")
        if (ptype, pid) in seen:
            continue
        seen.add((ptype, pid))
        normalized.append({"principal_type": ptype, "principal_id": pid})
    if visibility == VISIBILITY_SCOPED and not normalized:
        raise BadRequestError(message="指定范围可见时至少需要一条授权（用户/团队/角色）")

    row = (
        db.query(MarketplaceListingState)
        .filter(
            MarketplaceListingState.kind == kind,
            MarketplaceListingState.item_id == item_id,
        )
        .first()
    )
    if row is None:
        row = MarketplaceListingState(kind=kind, item_id=item_id, enabled=True)
        db.add(row)
    row.visibility = visibility
    row.updated_at = datetime.utcnow()
    row.updated_by = updated_by

    db.query(MarketplaceVisibilityGrant).filter(
        MarketplaceVisibilityGrant.kind == kind,
        MarketplaceVisibilityGrant.item_id == item_id,
    ).delete(synchronize_session=False)
    if visibility == VISIBILITY_SCOPED:
        for g in normalized:
            db.add(
                MarketplaceVisibilityGrant(
                    kind=kind,
                    item_id=item_id,
                    principal_type=g["principal_type"],
                    principal_id=g["principal_id"],
                    created_by=updated_by,
                )
            )
    db.commit()
    return {
        "kind": kind,
        "item_id": item_id,
        "visibility": visibility,
        "grants": normalized if visibility == VISIBILITY_SCOPED else [],
    }


def ensure_item_visible(
    db: Session, kind: str, item_id: str, user_id: Optional[str], *, resource: str
) -> None:
    """Detail/install path guard: when an item is invisible to the current user, treat it as "not found" (404, does not leak existence)."""
    from core.auth.marketplace_visibility import is_item_visible
    if not is_item_visible(db, kind, item_id, user_id):
        raise ResourceNotFoundError(resource, item_id)
