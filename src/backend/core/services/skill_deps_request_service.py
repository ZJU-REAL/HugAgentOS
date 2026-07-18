"""Skill dependency request lifecycle (strict verification version).

Flow: user imports an external skill -> ``detect_dependencies`` identifies declared
dependencies -> **actually probe them in the sandbox** (``skill_deps_prober``) to
confirm which packages are **truly missing**:

- All already present in the sandbox -> set ``dep_status='ready'`` directly, do not
  bother the admin;
- Packages truly missing -> skill ``dep_status='installing'`` (soft-disabled, excluded
  from runtime loading) + create a pending request (visible to admins on the
  "Sandbox Dependencies" admin page).

Two admin outcomes:
1. Rebuild the sandbox -> after the rebuild finishes, **probe again** package by
   package; only truly installed ones are set to ``satisfied`` and restored to
   ``ready``; still-missing ones stay pending (with a refreshed missing list) —
   no more blind approval.
2. Reject -> request set to ``rejected`` + reason recorded, skill
   ``dep_status='rejected'`` (stays soft-disabled), reason surfaced to the user.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.db.models import AdminSkill, SkillDependencyRequest
from core.services.skill_deps_prober import names_of, probe_still_missing

logger = logging.getLogger(__name__)

_DEP_KINDS = ("pip", "npm", "apt")


def _extract_declared(dependencies: Optional[dict]) -> dict:
    """Extract non-empty pip/npm/apt entries from a detect_dependencies result (ignore warnings)."""
    deps = dependencies or {}
    return {k: deps.get(k) or [] for k in _DEP_KINDS if deps.get(k)}


def _mark_ready(db: Session, skill: AdminSkill) -> None:
    """Skill dependencies satisfied: restore ready, and close out any leftover pending requests for it as satisfied."""
    if skill.dep_status != "ready":
        skill.dep_status = "ready"
    db.query(SkillDependencyRequest).filter(
        SkillDependencyRequest.skill_id == skill.skill_id,
        SkillDependencyRequest.status == "pending",
    ).update(
        {"status": "satisfied", "satisfied_at": datetime.utcnow()},
        synchronize_session=False,
    )


def _upsert_pending(db: Session, skill_id: str, *, user_id: Optional[str], missing: dict) -> None:
    req = (
        db.query(SkillDependencyRequest)
        .filter(
            SkillDependencyRequest.skill_id == skill_id,
            SkillDependencyRequest.status == "pending",
        )
        .first()
    )
    if req is None:
        req = SkillDependencyRequest(request_id=f"sdr_{uuid.uuid4().hex[:16]}", skill_id=skill_id)
        db.add(req)
    req.user_id = user_id
    req.missing = missing
    req.status = "pending"
    req.reason = None


async def gate_skill_deps(
    db: Session,
    skill_id: str,
    *,
    owner_user_id: Optional[str],
    dependencies: Optional[dict],
) -> bool:
    """Decide skill readiness based on **actual sandbox probing**. **Only applies to personally imported skills (owner_user_id non-empty)**.

    - No declared dependencies / global skill -> ``ready``, return False;
    - All already present in the sandbox -> ``ready``, return False (do not bother the admin);
    - Packages truly missing (or probe inconclusive, conservatively treated as missing) -> ``installing`` + pending, return True.
    """
    skill = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    if skill is None:
        return False

    declared = _extract_declared(dependencies)
    if not owner_user_id or not declared:
        _mark_ready(db, skill)
        db.commit()
        return False

    probed = await probe_still_missing(declared)
    if probed is None:
        # Probe inconclusive (sandbox unreachable / error) -> conservative: treat all declared deps as missing, hold pending for admin.
        missing = {k: names_of(v) for k, v in declared.items()}
        logger.warning("[skill-deps] %s probe inconclusive; conservatively flagging", skill_id)
    else:
        missing = {k: v for k, v in probed.items() if v}

    if not missing:
        _mark_ready(db, skill)
        db.commit()
        logger.info("[skill-deps] %s deps already satisfied in sandbox; ready", skill_id)
        return False

    skill.dep_status = "installing"
    _upsert_pending(db, skill_id, user_id=owner_user_id, missing=missing)
    db.commit()
    logger.info("[skill-deps] %s flagged installing; missing=%s", skill_id, missing)
    return True


def list_pending(db: Session) -> list[dict[str, Any]]:
    """List skills awaiting dependency installation (shown on the admin "Sandbox Dependencies" page, so admins know for whom and what to install)."""
    rows = (
        db.query(SkillDependencyRequest)
        .filter(SkillDependencyRequest.status == "pending")
        .order_by(SkillDependencyRequest.created_at)
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        sk = db.query(AdminSkill).filter(AdminSkill.skill_id == r.skill_id).first()
        out.append({
            "request_id": r.request_id,
            "skill_id": r.skill_id,
            "skill_name": sk.display_name if sk else r.skill_id,
            "user_id": r.user_id,
            "missing": r.missing or {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def reject_pending(
    db: Session,
    request_id: str,
    *,
    reason: Optional[str] = None,
    rejected_by: Optional[str] = None,
) -> bool:
    """Admin rejects a pending install request: request set to rejected + reason recorded, skill stays soft-disabled (dep_status='rejected').

    Returns True on successful rejection; returns False if no matching pending request is found. The (optional) reason is surfaced to the user.
    """
    req = (
        db.query(SkillDependencyRequest)
        .filter(
            SkillDependencyRequest.request_id == request_id,
            SkillDependencyRequest.status == "pending",
        )
        .first()
    )
    if req is None:
        return False
    req.status = "rejected"
    req.reason = (reason or "").strip() or None
    req.rejected_at = datetime.utcnow()
    req.rejected_by = rejected_by
    skill = db.query(AdminSkill).filter(AdminSkill.skill_id == req.skill_id).first()
    if skill is not None:
        skill.dep_status = "rejected"
    db.commit()
    logger.info("[skill-deps] request %s rejected by %s (skill=%s)", request_id, rejected_by, req.skill_id)
    return True


def get_reject_reason(db: Session, skill_id: str) -> Optional[str]:
    """Get the most recent rejection reason for a skill (for user-facing display like "Rejected by admin: ...")."""
    req = (
        db.query(SkillDependencyRequest)
        .filter(
            SkillDependencyRequest.skill_id == skill_id,
            SkillDependencyRequest.status == "rejected",
        )
        .order_by(SkillDependencyRequest.rejected_at.desc())
        .first()
    )
    return (req.reason if req else None) or None


async def verify_pending_after_rebuild(db: Session, run_id: str) -> dict[str, int]:
    """After a successful sandbox rebuild: **re-probe** the dependencies of each pending request's skill, one by one.

    Truly installed (no more missing packages in the sandbox) -> set satisfied + restore
    the skill to ready; still missing -> keep pending and refresh the missing list (this
    rebuild did not install it). Inconclusive probes also stay pending (conservative).
    Returns ``{"satisfied": n, "still_pending": m}``. Idempotent: returns all zeros when
    there are no pending requests.
    """
    rows = (
        db.query(SkillDependencyRequest)
        .filter(SkillDependencyRequest.status == "pending")
        .all()
    )
    if not rows:
        return {"satisfied": 0, "still_pending": 0}

    now = datetime.utcnow()
    satisfied = 0
    still_pending = 0
    for r in rows:
        skill = db.query(AdminSkill).filter(AdminSkill.skill_id == r.skill_id).first()
        declared = _extract_declared(skill.dependencies if skill else None)
        if not declared:
            # Skill no longer declares dependencies (edited/emptied) -> treat as satisfied.
            probed: Optional[dict] = {k: [] for k in _DEP_KINDS}
        else:
            probed = await probe_still_missing(declared)

        if probed is None:
            still_pending += 1
            continue
        missing = {k: v for k, v in probed.items() if v}
        if not missing:
            r.status = "satisfied"
            r.satisfied_at = now
            r.satisfied_by_run_id = run_id
            if skill is not None and skill.dep_status != "ready":
                skill.dep_status = "ready"
            satisfied += 1
        else:
            r.missing = missing
            still_pending += 1
    db.commit()
    logger.info(
        "[skill-deps] rebuild %s verified: satisfied=%d still_pending=%d",
        run_id, satisfied, still_pending,
    )
    return {"satisfied": satisfied, "still_pending": still_pending}
