"""Skill draft approve/reject — the single implementation shared by the AdminApp review endpoint and ConfigApp colleague-distillation save.

Extracted from api/routes/v1/admin_skill_drafts.py::approve_draft with behavior kept
identical; adds owner_user_id (whether the artifact is global or belongs to a specific
user) and edited_content (minor tweaks before saving).
The "immutable after approved/rejected" constraint is enforced here, naturally
preventing the two consoles from double-persisting.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.db.models import AdminSkill, AdminSkillDraft

logger = logging.getLogger(__name__)


def get_draft_or_404(db: Session, draft_id: str) -> AdminSkillDraft:
    d = db.query(AdminSkillDraft).filter(AdminSkillDraft.draft_id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return d


def approve_draft(
    db: Session,
    draft_id: str,
    *,
    enable_immediately: bool = False,
    reviewer_comment: Optional[str] = None,
    reviewer_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    edited_content: Optional[str] = None,
    publish_to_marketplace: bool = False,
    category: Optional[str] = None,
) -> Tuple[AdminSkillDraft, str]:
    """Approve a draft: materialize the skill artifact per decision. Returns (draft, skill_id).

    - decision='new_skill' with ``publish_to_marketplace=True`` → **publish to the skill
      marketplace** (submitted as approved, effective only once a user installs it). Must
      specify ``category`` (one of the marketplace's 8 fixed categories); in this mode
      ``enable_immediately`` / ``owner_user_id`` do not apply (marketplace items have no
      enable/ownership concept). — AdminApp pending-draft review takes this path.
    - decision='new_skill' with ``publish_to_marketplace=False`` → directly create a formal
      skill (optionally enable immediately / specify owner as a private skill). — ConfigApp
      colleague-distillation "confirm persist" takes this path.
    - decision='patch' → patch an existing formal skill in place (in-place update of a live skill).
    - edited_content non-empty → overwrite the draft content with it before persisting (only pending is editable)
    """
    d = get_draft_or_404(db, draft_id)
    if d.review_status != "pending":
        raise HTTPException(status_code=400, detail="draft_not_pending")

    if edited_content is not None and edited_content.strip():
        d.skill_content = edited_content

    skill_id = d.proposed_skill_id

    if d.decision == "new_skill" and publish_to_marketplace:
        # Approved new skill goes to the skill marketplace (not directly into the formal store). The marketplace publish commits internally.
        from core.services import marketplace_service

        result = marketplace_service.publish_skill_to_marketplace(
            db,
            skill_id=skill_id,
            skill_content=d.skill_content,
            display_name=d.display_name or skill_id,
            description=d.description or "",
            version=d.version or "0.1.0",
            tags=list(d.tags or []),
            category=category or "",
            submitter_name=(
                "同事蒸馏审核" if d.draft_kind == "colleague" else "蒸馏审核"
            ),
        )
        skill_id = result["skill_id"]
    elif d.decision == "new_skill":
        existing: Optional[AdminSkill] = (
            db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        )
        if existing:
            skill_id = f"{skill_id}-{uuid.uuid4().hex[:4]}"
        created_by = (
            "colleague_distill" if d.draft_kind == "colleague" else "distiller_approved"
        )
        skill = AdminSkill(
            skill_id=skill_id,
            skill_content=d.skill_content,
            display_name=d.display_name or skill_id,
            description=d.description or "",
            version=d.version or "0.1.0",
            tags=list(d.tags or []),
            allowed_tools=list(d.allowed_tools or []),
            extra_files={},
            is_enabled=bool(enable_immediately),
            owner_user_id=str(owner_user_id) if owner_user_id else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            created_by=created_by,
        )
        db.add(skill)
    elif d.decision == "patch":
        target_id = d.patch_target_id or skill_id
        target = db.query(AdminSkill).filter(AdminSkill.skill_id == target_id).first()
        if not target:
            raise HTTPException(status_code=400, detail=f"patch_target_missing:{target_id}")
        target.skill_content = d.skill_content
        if d.display_name:
            target.display_name = d.display_name
        if d.description:
            target.description = d.description
        if d.version:
            target.version = d.version
        if d.tags:
            target.tags = list(d.tags)
        if d.allowed_tools:
            target.allowed_tools = list(d.allowed_tools)
        target.updated_at = datetime.utcnow()
        skill_id = target_id
    else:
        raise HTTPException(status_code=400, detail=f"unsupported_decision:{d.decision}")

    d.review_status = "approved"
    d.reviewer_comment = reviewer_comment
    if reviewer_id:
        d.reviewer_id = reviewer_id
    d.reviewed_at = datetime.utcnow()
    d.updated_at = datetime.utcnow()

    db.commit()
    try:
        from core.agent_skills.cache_refresh import refresh_skill_caches
        refresh_skill_caches()
    except Exception as exc:
        logger.warning("skill_draft_service: cache refresh failed (%s)", exc)
    return d, skill_id


def reject_draft(
    db: Session,
    draft_id: str,
    *,
    rejected_reason: str,
    reviewer_id: Optional[str] = None,
) -> AdminSkillDraft:
    d = get_draft_or_404(db, draft_id)
    if d.review_status != "pending":
        raise HTTPException(status_code=400, detail="draft_not_pending")

    d.review_status = "rejected"
    d.rejected_reason = rejected_reason
    if reviewer_id:
        d.reviewer_id = reviewer_id
    d.reviewed_at = datetime.utcnow()
    d.updated_at = datetime.utcnow()
    db.commit()
    return d
