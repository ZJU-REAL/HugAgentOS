"""Who may actually load an evolved skill right now.

The release state machine models ``shadow → canary 5/25/100 → active`` and the
IR declares a ``scope``.  Neither meant anything at runtime: a materialised skill
was written as a global public ``AdminSkill``, and the capability resolver hands
every global public skill to every user.  So a brand-new, ``risk_tier=high``
capability the system wrote about itself went to 100% of users the moment it was
activated — more aggressive than the design it ships with claims to be.

This module is the missing enforcement point.  It answers one question —
*may this subject load this evolved skill?* — from the release record, and the
agent factory consults it on every run.

Three properties worth stating:

* **Fail closed.**  An evolved skill with no readable, in-flight release is not
  exposed.  The default for a capability the system authored about itself is
  "no", because the failure mode of guessing "yes" is that an unvetted change
  reaches everyone.
* **Shadow reaches nobody.**  That is what makes shadow zero-risk; a shadow
  stage that leaked to users would just be an unannounced canary.
* **Bucketing is stable per (release, subject).**  A user does not flip between
  versions mid-ramp, and the assignment is recomputable during analysis rather
  than stored per request.

Hand-authored skills are untouched: only ids carrying the evolution prefix are
ever filtered, so this cannot hide a capability a person created.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence, Set

from core.evolution.activation import EVOLUTION_PREFIX

logger = logging.getLogger(__name__)


def is_evolved_skill_id(skill_id: str) -> bool:
    return str(skill_id or "").startswith(EVOLUTION_PREFIX)


def _latest_releases_by_asset(db, asset_ids: Sequence[str]) -> Dict[str, object]:
    """The most recent release per asset.

    An asset may accumulate several release rows over its life (activation,
    rollback, re-activation). Only the newest one describes the current state;
    an older ``active`` row must not resurrect a rolled-back skill.
    """
    from core.db.models.evolution import EvolutionRelease

    rows = (
        db.query(EvolutionRelease)
        .filter(
            EvolutionRelease.target_kind == "skill",
            EvolutionRelease.target_asset_id.in_(list(asset_ids)),
        )
        .order_by(EvolutionRelease.created_at.asc())
        .all()
    )
    latest: Dict[str, object] = {}
    for row in rows:
        latest[row.target_asset_id] = row
    return latest


def _scope_permits(release, *, user_id: str, tenant_id: str) -> bool:
    """Whether the release's declared scope covers this subject.

    ``scope_filter`` is written at activation from the candidate's IR. An empty
    filter means deployment-wide, which is only reachable for a candidate whose
    IR asked for it.
    """
    scope = release.scope_filter or {}
    owner = str(scope.get("owner_user_id") or "")
    if owner:
        return owner == user_id
    tenant = str(scope.get("tenant_id") or "")
    if tenant and tenant != (tenant_id or "default"):
        return False
    return True


def visible_evolved_skill_ids(
    db,
    candidate_ids: Iterable[str],
    *,
    user_id: str,
    tenant_id: str = "default",
) -> Set[str]:
    """Of ``candidate_ids``, the evolved skill ids this subject may load."""
    from core.evolution.release import ACTIVE, CANARY, in_canary_bucket

    evolved = {sid for sid in candidate_ids if is_evolved_skill_id(sid)}
    if not evolved:
        return set()

    try:
        latest = _latest_releases_by_asset(db, sorted(evolved))
    except Exception as exc:  # noqa: BLE001
        # Fail closed: without release state we cannot show that a change was
        # vetted, and an unvetted self-authored capability must not be loaded.
        logger.warning("[exposure] release lookup failed, withholding evolved skills: %s", exc)
        return set()

    visible: Set[str] = set()
    for skill_id in sorted(evolved):
        release = latest.get(skill_id)
        if release is None:
            continue
        if not _scope_permits(release, user_id=user_id, tenant_id=tenant_id):
            continue
        stage = str(release.stage or "")
        if stage == ACTIVE:
            visible.add(skill_id)
        elif stage == CANARY:
            if in_canary_bucket(
                release_id=release.release_id,
                subject_id=user_id,
                traffic_percent=int(release.traffic_percent or 0),
            ):
                visible.add(skill_id)
        # shadow / draft / replay_passed / rejected / rolled_back / retired:
        # not exposed. Shadow in particular is defined by reaching nobody.
    return visible


def filter_skill_ids(
    skill_ids: Sequence[str],
    *,
    user_id: str,
    tenant_id: str = "default",
    db=None,
) -> List[str]:
    """Drop evolved skills this subject is not entitled to; keep everything else.

    Called on the request path, so it does nothing at all when the list contains
    no evolved ids — the common case, and one that must not cost a query.
    """
    ids = [sid for sid in skill_ids if sid]
    if not any(is_evolved_skill_id(sid) for sid in ids):
        return list(ids)

    def _resolve(session) -> Set[str]:
        return visible_evolved_skill_ids(
            session, ids, user_id=user_id or "", tenant_id=tenant_id or "default"
        )

    try:
        if db is not None:
            allowed = _resolve(db)
        else:
            from core.db.engine import SessionLocal

            with SessionLocal() as session:
                allowed = _resolve(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[exposure] gate failed, withholding evolved skills: %s", exc)
        allowed = set()

    return [sid for sid in ids if not is_evolved_skill_id(sid) or sid in allowed]


def exposure_state(db, skill_id: str) -> Dict[str, object]:
    """Human-readable exposure of one evolved skill, for the console."""
    from core.evolution.release import ACTIVE, CANARY, SHADOW

    latest = _latest_releases_by_asset(db, [skill_id])
    release = latest.get(skill_id)
    if release is None:
        return {"skill_id": skill_id, "stage": "none", "exposed_to": "nobody"}
    stage = str(release.stage or "")
    if stage == ACTIVE:
        reach = "all users in scope"
    elif stage == CANARY:
        reach = f"{int(release.traffic_percent or 0)}% of users in scope"
    elif stage == SHADOW:
        reach = "nobody (shadow)"
    else:
        reach = "nobody"
    return {
        "skill_id": skill_id,
        "release_id": release.release_id,
        "stage": stage,
        "traffic_percent": int(release.traffic_percent or 0),
        "scope": release.scope_filter or {},
        "exposed_to": reach,
    }
