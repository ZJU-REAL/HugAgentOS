"""Marketplace item visibility-scope resolution (single source of truth shared across the skill / plugin / sub-agent marketplaces).

The decision of "which items a given user can see in the marketplace" is funneled into this
module, ensuring the visible set seen across the three paths — marketplace listing, detail, and
install — is fully consistent, preventing the privilege escalation of "hidden in the listing but
still installable by guessing the slug".

**Visible-to-all-by-default / blacklist-exception model** (the opposite direction of the knowledge base's whitelist model):
  - ``marketplace_listing_states.visibility`` missing a row or ``public`` → visible to everyone;
  - ``scoped`` → visible only to principals granted in ``marketplace_visibility_grants``,
    ``principal_type`` = ``user`` | ``team`` | ``role``, visible if any one matches (union);
  - Super admins are always visible. Role principals include both personally-assigned roles and department default roles obtained via teams
    (consistent with the capability-bit resolution rules of core/auth/role_permissions.py).

Governs only marketplace browsing and installation; does not retroactively track installed instances (later restrictions do not revoke what is already installed).

CE compatibility: ``marketplace_visibility_grants`` is an EE-only table, not created in CE; and CE
has no admin marketplace route, so no scoped rows are produced. This module wraps all grant/role
table queries in try/except fallbacks — when a table is missing it is treated as "no grant"
(scoped items are invisible, but CE in practice has no scoped rows).
"""

from __future__ import annotations

from typing import Optional, Set

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.auth.kb_permissions import _is_super_admin, _user_team_ids


def _user_role_ids(db: Session, user_id: str, team_ids: Set[str]) -> Set[str]:
    """The user's effective role set: personally-assigned ∪ department default roles of each team. Table missing (CE) → empty set."""
    try:
        from core.db.models import RoleAssignment
        conds = [
            (RoleAssignment.principal_type == "user")
            & (RoleAssignment.principal_id == user_id)
        ]
        if team_ids:
            conds.append(
                (RoleAssignment.principal_type == "team")
                & (RoleAssignment.principal_id.in_(list(team_ids)))
            )
        rows = db.query(RoleAssignment.role_id).filter(or_(*conds)).all()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _scoped_item_ids(db: Session, kind: str) -> Set[str]:
    """The set of item_ids in this marketplace set to scoped (visible to a specified scope). Missing column / query failure → empty set."""
    try:
        from core.db.models import MarketplaceListingState
        rows = (
            db.query(MarketplaceListingState.item_id)
            .filter(
                MarketplaceListingState.kind == kind,
                MarketplaceListingState.visibility == "scoped",
            )
            .all()
        )
        return {r[0] for r in rows}
    except Exception:
        return set()


def _granted_item_ids(db: Session, kind: str, scoped_ids: Set[str], user_id: str) -> Set[str]:
    """The set of item_ids among scoped items for which the current user is granted access via any user/team/role principal."""
    try:
        from core.db.models import MarketplaceVisibilityGrant as G
        team_ids = _user_team_ids(db, user_id)
        role_ids = _user_role_ids(db, user_id, team_ids)
        conds = [(G.principal_type == "user") & (G.principal_id == user_id)]
        if team_ids:
            conds.append((G.principal_type == "team") & (G.principal_id.in_(list(team_ids))))
        if role_ids:
            conds.append((G.principal_type == "role") & (G.principal_id.in_(list(role_ids))))
        rows = (
            db.query(G.item_id)
            .filter(G.kind == kind, G.item_id.in_(list(scoped_ids)))
            .filter(or_(*conds))
            .all()
        )
        return {r[0] for r in rows}
    except Exception:
        return set()


def get_hidden_item_ids(db: Session, kind: str, user_id: Optional[str]) -> Set[str]:
    """The set of item_ids in this marketplace that are **invisible** to the current user (for batch filtering, one query on the listing path).

    Zero extra overhead when there are no scoped items (the norm for the vast majority of deployments); always an empty set for super admins.
    An empty ``user_id`` (anonymous / system call) is treated as an ordinary unauthorized user.
    """
    scoped = _scoped_item_ids(db, kind)
    if not scoped:
        return set()
    user_id = str(user_id or "")
    if user_id and _is_super_admin(db, user_id):
        return set()
    granted = _granted_item_ids(db, kind, scoped, user_id) if user_id else set()
    return scoped - granted


def is_item_visible(db: Session, kind: str, item_id: str, user_id: Optional[str]) -> bool:
    """Single-item visibility (for guarding the detail / install paths)."""
    return str(item_id) not in get_hidden_item_ids(db, kind, user_id)
