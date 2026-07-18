"""Role capability bundles: normalize / merge / aggregate per user.

A role ([[Role]]) is a reusable, named **capability-grant bundle** — a set of
capability bits packaged together and assigned to a team (= department default
role, inherited by members in real time) or an individual. This module is the
single source of truth for "role → capability bits" and the sole extension seam
for a "later rework into an org tree" (see the note at the end of
``role_permissions_for_user``).

Resolution chain (see ``core/auth/capabilities.py``):
    personal explicit override → **union of roles** → team default → system default

Role semantics are "grants" (additive):

- Boolean bits only store what is granted (``True``); multiple roles take the
  union (a grant from any role takes effect), reusing the union logic of
  ``capabilities.merge_team_permissions``.
- ``allowed_apps`` takes the union of each role's whitelist (more permissive).

Defensive degradation: CE (no roles table) / any query exception → return ``{}``;
resolution degrades to "personal → team default → system default", exactly as
before roles were introduced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS, merge_team_permissions


def normalize_role_permissions(payload: Any) -> Dict[str, Any]:
    """Normalize a role capability bundle: keep only valid capability bits, with "grant" semantics.

    - Boolean bits: store ``True`` only when the payload key is truthy (a role
      never expresses "off" — additive grants; "off" is left to personal explicit
      overrides);
    - ``allowed_apps``: ``list`` → de-duplicated string list (empty list =
      restricted to "none"); key absent = the role does not restrict app visibility.
    """
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in BOOL_CAPABILITY_DEFAULTS:
        if payload.get(key):
            out[key] = True
    raw_apps = payload.get("allowed_apps")
    if isinstance(raw_apps, list):
        out["allowed_apps"] = list(dict.fromkeys(str(x) for x in raw_apps))
    return out


def merge_role_permissions(raw_perms_list: Any) -> Dict[str, Any]:
    """Merge multiple roles' capability bundles (union across roles / most permissive).

    Directly reuses the team-default union logic: boolean bits are "True if any is
    True", ``allowed_apps`` takes the union.
    """
    return merge_team_permissions(raw_perms_list)


def role_permissions_for_user(
    db: Session, user_id: str, team_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Aggregate the union of capability bundles across all of a user's roles (direct assignments + department default roles of their teams).

    - Direct roles: ``role_assignments(principal_type='user', principal_id=user_id)``
    - Department default roles: ``role_assignments(principal_type='team', principal_id=team_id)``
      for each team the user belongs to — inherited by members in real time (no
      provisioning needed; new SSO members take effect immediately).

    ``team_ids`` can be passed in by callers that already loaded the teams
    (login/session serialization), saving one team_members query.
    No roles / CE without tables / any exception → ``{}`` (resolution degrades to
    "personal → team default → system default").
    """
    try:
        from core.db.models import Role, RoleAssignment, TeamMember

        if team_ids is None:
            team_ids = [
                tid for (tid,) in db.query(TeamMember.team_id).filter(TeamMember.user_id == user_id).all()
            ]

        conds = [
            (RoleAssignment.principal_type == "user") & (RoleAssignment.principal_id == user_id)
        ]
        if team_ids:
            conds.append(
                (RoleAssignment.principal_type == "team")
                & (RoleAssignment.principal_id.in_(team_ids))
            )

        from sqlalchemy import or_

        role_ids = [
            rid
            for (rid,) in db.query(RoleAssignment.role_id).filter(or_(*conds)).distinct().all()
        ]
        if not role_ids:
            return {}
        perms_list = [
            p for (p,) in db.query(Role.permissions).filter(Role.role_id.in_(role_ids)).all()
        ]
    except Exception:  # noqa: BLE001 — CE without role tables / any exception degrades safely
        return {}
    return merge_role_permissions(perms_list)
