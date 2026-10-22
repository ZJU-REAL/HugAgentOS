"""skill submissions responsibilities."""
from __future__ import annotations
import json
import uuid
from typing import Any, Dict, List, Optional
from core.db.models import AdminSkill
from features.marketplace.models import MarketplaceSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from fastapi import HTTPException
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.marketplace import skill_metadata
from features.marketplace import skill_submission_data
from features.marketplace import skills_state

def submit_to_marketplace(
    db: Session,
    skill_id: str,
    *,
    owner_user_id: str,
    submitter_name: str = "",
    note: str = "",
    category: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """User submits their private skill for marketplace listing (content snapshot, pending admin review)."""
    row = (
        db.query(AdminSkill)
        .filter(AdminSkill.skill_id == skill_id, AdminSkill.owner_user_id == owner_user_id)
        .first()
    )
    if row is None:
        raise ResourceNotFoundError("skill", skill_id)

    active = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.skill_id == skill_id,
            MarketplaceSubmission.owner_user_id == owner_user_id,
            MarketplaceSubmission.status.in_(["pending", "approved"]),
        )
        .first()
    )
    if active is not None:
        state = "待审核" if active.status == "pending" else "已上架"
        raise HTTPException(status_code=409, detail=f"该技能已有{state}的上架申请")

    # The snapshot excludes the credentials file: when installing a market
    # skill with secrets, _inject_secrets writes the user's API key in
    # plaintext into extra_files[SECRETS_FILENAME] — that must never be
    # distributed to all installers with the listing snapshot. The credential
    # field names are kept with the snapshot as a sentinel entry (without
    # values), so installers are prompted to enter their own keys; SKILL.md is
    # correspondingly stripped of the injected section.
    snapshot_files = dict(row.extra_files or {})
    submitter_secrets = snapshot_files.pop(skills_state.SECRETS_FILENAME, None)
    snapshot_content = row.skill_content
    if submitter_secrets:
        snapshot_content = skill_submission_data.strip_injected_secrets(row.skill_content or "")
        try:
            secret_keys = sorted(json.loads(submitter_secrets).keys())
        except (TypeError, ValueError, AttributeError):
            secret_keys = []
        if secret_keys:
            # The original required/label metadata is unavailable in the installed artifact; approximate as "all required"
            snapshot_files[skills_state.REQUIRED_SECRETS_SNAPSHOT] = json.dumps(
                [{"key": k, "label": k, "required": True} for k in secret_keys],
                ensure_ascii=False,
            )

    now = utc_now()
    sub = MarketplaceSubmission(
        submission_id=f"mksub_{uuid.uuid4().hex[:16]}",
        slug=skill_submission_data.derive_slug(db, skill_id, owner_user_id),
        skill_id=skill_id,
        owner_user_id=owner_user_id,
        submitter_name=(submitter_name or "").strip(),
        display_name=row.display_name or skill_id,
        summary=(summary or "").strip() or (row.description or ""),
        user_intro=str(row.user_intro or "").strip() or None,
        category=skill_metadata.validate_category(category),
        tags=list(row.tags or []),
        version=row.version or "1.0.0",
        note=(note or "").strip(),
        skill_content=snapshot_content,
        extra_files=snapshot_files,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info(
        "marketplace_submission_created: id=%s skill=%s owner=%s slug=%s",
        sub.submission_id, skill_id, owner_user_id, sub.slug,
    )
    return skill_submission_data.submission_to_dict(sub)


def list_my_submissions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    """All of the current user's listing submissions (newest → oldest)."""
    rows = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.owner_user_id == owner_user_id)
        .order_by(MarketplaceSubmission.created_at.desc())
        .all()
    )
    return [skill_submission_data.submission_to_dict(r) for r in rows]


def withdraw_submission(db: Session, submission_id: str, owner_user_id: str) -> None:
    """User withdraws their own submission: pending is deleted directly; approved requires an admin to delist; rejected can be deleted and resubmitted."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.submission_id == submission_id,
            MarketplaceSubmission.owner_user_id == owner_user_id,
        )
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    if sub.status == "approved":
        raise BadRequestError(message="技能已上架，如需下架请联系管理员")
    db.delete(sub)
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Admin view: all listing submissions, filterable by status (pending first, newest → oldest)."""
    q = db.query(MarketplaceSubmission)
    if status:
        q = q.filter(MarketplaceSubmission.status == status)
    rows = q.order_by(MarketplaceSubmission.created_at.desc()).all()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    rows.sort(key=lambda r: order.get(r.status, 3))
    return [skill_submission_data.submission_to_dict(r) for r in rows]


def get_submission(db: Session, submission_id: str) -> Dict[str, Any]:
    """Submission detail for admin review (including a SKILL.md body preview and the attachment list)."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    return skill_submission_data.submission_to_dict(sub, with_content=True)


def review_submission(
    db: Session,
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
) -> Dict[str, Any]:
    """Admin review: approval means listing (appears in the market list); rejecting pending = decline, rejecting approved = delist."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    # State-machine guard: approve only allows pending→approved (prevents two
    # concurrent admins / a stale list mis-click from listing a just-rejected
    # submission directly); reject allows pending (decline) and approved
    # (delist).
    if approve and sub.status != "pending":
        state = "已上架" if sub.status == "approved" else "已驳回"
        raise HTTPException(status_code=409, detail=f"该申请{state}，不能重复审核通过（如需重新上架请让用户重新提交）")
    if not approve and sub.status == "rejected":
        raise HTTPException(status_code=409, detail="该申请已是驳回状态")
    sub.status = "approved" if approve else "rejected"
    sub.review_note = (review_note or "").strip()
    if approve and (category or "").strip():
        sub.category = skill_metadata.validate_category(category)
    sub.reviewed_at = utc_now()
    sub.updated_at = utc_now()
    db.commit()
    logger.info(
        "marketplace_submission_reviewed: id=%s slug=%s status=%s",
        sub.submission_id, sub.slug, sub.status,
    )
    return skill_submission_data.submission_to_dict(sub)
