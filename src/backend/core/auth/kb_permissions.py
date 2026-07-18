"""Knowledge-base access permission resolution (single source of truth for the shared-KB permission-grant system).

Every determination of "which knowledge bases a user can see/retrieve, and at what
permission level" funnels through this module, ensuring:
  - capability catalog display (catalog.py)
  - agent retrieval (retrieve_local_kb / retrieve_dataset_content)
  - read/write validation (kb.py / kb_service.py)
all see exactly the same visible set — eliminating the privilege escalation of
"hidden in the UI but retrievable by the agent".

Resources come in two kinds, uniformly identified by ``resource_id``:
  - local shared KBs: ``kb_id`` (created in the admin console, owned by the system
    owner, ``KBSpace.visibility`` not private)
  - external Dify KBs: ``dataset_id``

**Hidden-by-default / whitelist model (permissions are assigned only in "User
management / Team management"; KB management assigns no permissions)**:
  - Shared KBs are **hidden from everyone by default**; only granted users/teams see them at their level.
  - Precedence: **a personal grant overrides a team grant** (when a user has both a
    personal grant and a team-mediated grant on a KB, the personal one applies).
  - Owner / super admin are always admin; a private user KB is visible only to its owner.

Permission tiers (modeled on team-folder permissions admin>edit>view>none):
view < edit < admin. view already means visible and retrievable.

CE compatibility: ``kb_grants`` is an EE-only table, not created in CE. All queries
against it in this module are wrapped in try/except fallbacks — when the table is
missing, treat as "no grants" (shared KBs invisible to regular users; private KBs
still visible to their owner).
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Set

from sqlalchemy import or_
from sqlalchemy.orm import Session

KBLevel = Literal["none", "view", "edit", "admin"]

_RANK: Dict[str, int] = {"none": 0, "view": 1, "edit": 2, "admin": 3}


def _max_level(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def has_kb_permission(current: str, required: str) -> bool:
    return _RANK.get(current, 0) >= _RANK.get(required, 0)


def is_shared_visibility(visibility: str) -> bool:
    """Shared-KB determination: anything non-private is a shared KB (globally retrievable by kb_id); only private is owner-isolated.

    Retrieval classification and catalog visibility share this single definition,
    avoiding scattered visibility-string checks at call sites that would drift apart.
    """
    return visibility != "private"


def level_to_caps(level: str) -> Dict[str, bool]:
    """Grant level → frontend capability bits (shared policy between catalog items and other consumers)."""
    return {
        "editable": level == "admin",
        "deletable": level == "admin",
        "uploadable": level in ("edit", "admin"),
    }


# ── Principals (user + their teams + super-admin status) ────────────────────

def _is_super_admin(db: Session, user_id: str) -> bool:
    try:
        from core.db.models import UserShadow
        row = db.query(UserShadow.extra_data).filter(UserShadow.user_id == user_id).first()
        meta = row[0] if row and isinstance(row[0], dict) else {}
        return str(meta.get("role") or "") == "super_admin"
    except Exception:
        return False


def _user_team_ids(db: Session, user_id: str) -> Set[str]:
    try:
        from core.db.models import TeamMember
        rows = db.query(TeamMember.team_id).filter(TeamMember.user_id == user_id).all()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _effective_grants(db: Session, resource_type: str, user_id: str, team_ids: Set[str]) -> Dict[str, str]:
    """Return {resource_id: the user's effective grant level}, with **personal grants taking precedence over team grants**.

    If a resource has both a personal grant and a team-mediated grant → take the
    personal one; multiple team grants → take the highest. Table missing → {}.
    """
    user_grants: Dict[str, str] = {}
    team_grants: Dict[str, str] = {}
    try:
        from core.db.models import KBGrant
        conds = [(KBGrant.principal_type == "user") & (KBGrant.principal_id == user_id)]
        if team_ids:
            conds.append((KBGrant.principal_type == "team") & (KBGrant.principal_id.in_(list(team_ids))))
        rows = (
            db.query(KBGrant.resource_id, KBGrant.principal_type, KBGrant.level)
            .filter(KBGrant.resource_type == resource_type)
            .filter(or_(*conds))
            .all()
        )
        for resource_id, principal_type, level in rows:
            lvl = level if level in _RANK else "view"
            if principal_type == "user":
                user_grants[resource_id] = lvl  # personal grant is unique (PK), take directly
            else:
                team_grants[resource_id] = _max_level(team_grants.get(resource_id, "none"), lvl)
    except Exception:
        pass
    # Personal overrides team
    effective = dict(team_grants)
    effective.update(user_grants)
    return effective


# ── Local KBs ────────────────────────────────────────────────────────────────

def _level_for(
    owner: str, user_id: str, is_admin: bool, visibility: str, grant: Optional[str],
) -> Optional[KBLevel]:
    """Single KB → effective level (None means invisible). The batch and single-KB paths share this rule (hidden-by-default / whitelist).

    Owner/super admin are always admin; private is owner-only; shared KBs (non-private)
    are **hidden from everyone by default** — only granted users/teams see them at their
    grant level (``grant``, None = not granted → invisible). Personal grants take
    precedence over team grants.
    """
    if is_admin or owner == user_id:
        return "admin"
    if visibility == "private":
        return None
    # Shared KB: hidden by default; only grantees see it at their level
    return grant  # type: ignore[return-value]


def get_accessible_local_kb_levels(db: Session, user_id: str) -> Dict[str, KBLevel]:
    """Return local KBs as {kb_id: effective level} (KBs the user can at least view). Rules in ``_level_for``."""
    user_id = str(user_id or "")
    levels: Dict[str, KBLevel] = {}
    if not user_id:
        return levels

    from core.db.models import KBSpace

    is_admin = _is_super_admin(db, user_id)
    team_ids = _user_team_ids(db, user_id)
    effective = _effective_grants(db, "local", user_id, team_ids)

    rows = (
        db.query(KBSpace.kb_id, KBSpace.visibility, KBSpace.user_id)
        .filter(KBSpace.deleted_at.is_(None))
        .all()
    )
    for kb_id, visibility, owner in rows:
        lvl = _level_for(owner, user_id, is_admin, visibility, effective.get(kb_id))
        if lvl:
            levels[kb_id] = lvl
    return levels


def get_accessible_local_kb_ids(db: Session, user_id: str) -> Set[str]:
    return set(get_accessible_local_kb_levels(db, user_id).keys())


# ── Dify datasets ────────────────────────────────────────────────────────────

def get_dataset_levels(db: Session, user_id: str, dataset_ids: List[str]) -> Dict[str, KBLevel]:
    """For the given list of Dify dataset ids, return {dataset_id: effective level} (accessible ones only).

    Hidden-by-default / whitelist model: only granted datasets are visible (personal
    grants take precedence over team grants); super admin is always admin.
    """
    user_id = str(user_id or "")
    out: Dict[str, KBLevel] = {}
    if not user_id or not dataset_ids:
        return out

    is_admin = _is_super_admin(db, user_id)
    team_ids = _user_team_ids(db, user_id)
    effective = _effective_grants(db, "dify", user_id, team_ids) if not is_admin else {}

    for ds_id in dataset_ids:
        ds_id = str(ds_id or "").strip()
        if not ds_id:
            continue
        if is_admin:
            out[ds_id] = "admin"
        else:
            lvl = effective.get(ds_id)
            if lvl:
                out[ds_id] = lvl  # type: ignore[assignment]
    return out


# ── Mixed filtering (used by agent_factory: enabled_kb_ids mixes local kb_ and dify dataset kinds) ──

def filter_accessible_kb_ids(db: Session, user_id: str, kb_ids: List[str]) -> List[str]:
    """Strip ids the current user cannot access from the client-supplied enabled_kb_ids, preserving order.

    Local KB ids are distinguished by the ``kb_`` prefix; the rest are treated as Dify
    datasets. Guards against the frontend passing unauthorized ids.
    """
    if not kb_ids:
        return []
    local_ids = [x for x in kb_ids if str(x).startswith("kb_")]
    dify_ids = [x for x in kb_ids if not str(x).startswith("kb_")]

    allowed: Set[str] = set()
    if local_ids:
        local_levels = get_accessible_local_kb_levels(db, user_id)
        allowed |= {k for k in local_ids if k in local_levels}
    if dify_ids:
        ds_levels = get_dataset_levels(db, user_id, dify_ids)
        allowed |= set(ds_levels.keys())
    return [x for x in kb_ids if x in allowed]


def resolve_local_kb_level(db: Session, user_id: str, kb_id: str) -> KBLevel:
    """Permission level for a single KB (used by kb.py / kb_service read/write validation; single-KB query, avoids a full-table scan)."""
    user_id = str(user_id or "")
    if not user_id or not kb_id:
        return "none"
    from core.db.models import KBSpace

    row = (
        db.query(KBSpace.kb_id, KBSpace.visibility, KBSpace.user_id)
        .filter(KBSpace.kb_id == kb_id, KBSpace.deleted_at.is_(None))
        .first()
    )
    if not row:
        return "none"

    _, visibility, owner = row
    is_admin = _is_super_admin(db, user_id)
    grant: Optional[str] = None
    # Only look up grants when "not owner / not super admin / shared KB" (hidden by default; ungranted means none)
    if not is_admin and owner != user_id and visibility != "private":
        grant = _effective_grants(db, "local", user_id, _user_team_ids(db, user_id)).get(kb_id)
    return _level_for(owner, user_id, is_admin, visibility, grant) or "none"
