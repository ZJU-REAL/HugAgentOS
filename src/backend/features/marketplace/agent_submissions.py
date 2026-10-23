"""agent submissions responsibilities."""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional
from features.marketplace.models import AgentMarketSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from features.marketplace.agent_categories import validate_category
from fastapi import HTTPException
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.marketplace import agent_submission_data

def submit_to_marketplace(
    db: Session,
    agent_id: str,
    *,
    owner_user_id: str,
    submitter_name: str = "",
    note: str = "",
    category: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """A user submits their self-built sub-agent for marketplace listing (content snapshot, pending admin review)."""
    agent = agent_submission_data.owned_agent(db, agent_id, owner_user_id)

    active = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.agent_id == agent_id,
            AgentMarketSubmission.owner_user_id == owner_user_id,
            AgentMarketSubmission.status.in_(["pending", "approved"]),
        )
        .first()
    )
    if active is not None:
        state = "待审核" if active.status == "pending" else "已上架"
        raise HTTPException(status_code=409, detail=f"该子智能体已有{state}的上架申请")

    sub = agent_submission_data.new_submission(
        submission_id=f"asub_{uuid.uuid4().hex[:16]}",
        slug=agent_submission_data.derive_slug(db, agent.name),
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        submitter_name=submitter_name,
        name=agent.name,
        avatar=agent.avatar,
        description=agent.description or "",
        summary=(summary or "").strip() or (agent.description or ""),
        category=validate_category(category),
        note=note,
        system_prompt=agent.system_prompt or "",
        welcome_message=agent.welcome_message or "",
        suggested_questions=agent.suggested_questions,
        model_config_snapshot=agent_submission_data.model_config_snapshot(agent),
        bindings_snapshot=agent_submission_data.bindings_snapshot(agent),
        tags=list((agent.extra_config or {}).get("ontology_tags") or []),
        status="pending",
    )
    db.add(sub)
    db.commit()
    logger.info(
        "agent_market_submission_created: id=%s agent=%s owner=%s slug=%s",
        sub.submission_id,
        agent_id,
        owner_user_id,
        sub.slug,
    )
    return agent_submission_data.submission_to_dict(sub)


def list_my_submissions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    rows = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.owner_user_id == owner_user_id)
        .order_by(AgentMarketSubmission.created_at.desc())
        .all()
    )
    return [agent_submission_data.submission_to_dict(r) for r in rows]


def withdraw_submission(db: Session, submission_id: str, owner_user_id: str) -> None:
    """A user withdraws their own submission: pending/rejected can be deleted; approved requires an admin to delist."""
    sub = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.submission_id == submission_id,
            AgentMarketSubmission.owner_user_id == owner_user_id,
        )
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    if sub.status == "approved":
        raise BadRequestError(message="子智能体已上架，如需下架请联系管理员")
    db.delete(sub)
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Admin view: all listing submissions, filterable by status (pending first, newest → oldest)."""
    q = db.query(AgentMarketSubmission)
    if status:
        q = q.filter(AgentMarketSubmission.status == status)
    rows = q.order_by(AgentMarketSubmission.created_at.desc()).all()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    rows.sort(key=lambda r: order.get(r.status, 3))
    return [agent_submission_data.submission_to_dict(r) for r in rows]


def get_submission(db: Session, submission_id: str) -> Dict[str, Any]:
    sub = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    return agent_submission_data.submission_to_dict(sub, with_content=True)


def review_submission(
    db: Session,
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
) -> Dict[str, Any]:
    """Admin review: approval lists it immediately; rejecting pending = refuse, rejecting approved = delist."""
    sub = (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("agent_market_submission", submission_id)
    if approve and sub.status != "pending":
        state = "已上架" if sub.status == "approved" else "已驳回"
        raise HTTPException(
            status_code=409,
            detail=f"该申请{state}，不能重复审核通过（如需重新上架请让用户重新提交）",
        )
    if not approve and sub.status == "rejected":
        raise HTTPException(status_code=409, detail="该申请已是驳回状态")
    sub.status = "approved" if approve else "rejected"
    sub.review_note = (review_note or "").strip()
    if approve and (category or "").strip():
        sub.category = validate_category(category)
    sub.reviewed_at = utc_now()
    sub.updated_at = utc_now()
    db.commit()
    logger.info(
        "agent_market_submission_reviewed: id=%s slug=%s status=%s",
        sub.submission_id,
        sub.slug,
        sub.status,
    )
    return agent_submission_data.submission_to_dict(sub)
