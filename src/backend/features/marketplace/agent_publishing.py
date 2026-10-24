"""agent publishing responsibilities."""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional
from features.marketplace.models import AgentMarketSubmission
from core.db.models import UserAgent
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from features.marketplace import edition as ml
from features.marketplace.agent_categories import validate_category
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
import logging
logger = logging.getLogger(__name__)
from features.marketplace import agent_catalog
from features.marketplace import agent_submission_data
from features.marketplace import agents_state

def publish_agent_to_market(
    db: Session,
    agent_id: str,
    *,
    category: str,
    summary: str = "",
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """An admin lists an existing sub-agent directly on the marketplace as approved (no review).

    If the same agent_id already has an admin listing record, update it (keeping the original slug); otherwise create a new one.
    """
    category = validate_category(category)
    agent = db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()
    if agent is None:
        raise ResourceNotFoundError("user_agent", agent_id)

    now = utc_now()
    existing = (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.agent_id == agent_id,
            AgentMarketSubmission.owner_user_id == agents_state.ADMIN_UPLOAD_OWNER,
        )
        .first()
    )
    summary = (summary or "").strip() or (agent.description or "")
    if existing is not None:
        existing.name = agent.name
        existing.avatar = agent.avatar
        existing.description = agent.description or ""
        existing.summary = summary
        existing.category = category
        existing.system_prompt = agent.system_prompt or ""
        existing.welcome_message = agent.welcome_message or ""
        existing.suggested_questions = list(agent.suggested_questions or [])
        existing.model_config_snapshot = agent_submission_data.model_config_snapshot(agent)
        existing.bindings_snapshot = agent_submission_data.bindings_snapshot(agent)
        existing.tags = list((agent.extra_config or {}).get("ontology_tags") or [])
        existing.status = "approved"
        existing.review_note = ""
        existing.reviewed_at = now
        existing.updated_at = now
        flag_modified(existing, "suggested_questions")
        flag_modified(existing, "model_config_snapshot")
        flag_modified(existing, "bindings_snapshot")
        flag_modified(existing, "tags")
        db.commit()
        return {"slug": existing.slug, "action": "updated", "message": "子智能体市场内容已更新"}

    sub = agent_submission_data.new_submission(
        submission_id=f"aadm_{uuid.uuid4().hex[:16]}",
        slug=agent_submission_data.derive_slug(db, agent.name),
        agent_id=agent_id,
        owner_user_id=agents_state.ADMIN_UPLOAD_OWNER,
        submitter_name=submitter_name,
        name=agent.name,
        avatar=agent.avatar,
        description=agent.description or "",
        summary=summary,
        category=category,
        note="管理员上传，免审核直接上架",
        system_prompt=agent.system_prompt or "",
        welcome_message=agent.welcome_message or "",
        suggested_questions=agent.suggested_questions,
        model_config_snapshot=agent_submission_data.model_config_snapshot(agent),
        bindings_snapshot=agent_submission_data.bindings_snapshot(agent),
        tags=list((agent.extra_config or {}).get("ontology_tags") or []),
        status="approved",
        reviewed_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info("agent_market_admin_published: agent=%s slug=%s", agent_id, sub.slug)
    return {"slug": sub.slug, "action": "published", "message": "子智能体已上架市场"}


def create_market_agent(
    db: Session,
    *,
    name: str,
    category: str,
    avatar: str = "",
    description: str = "",
    summary: str = "",
    system_prompt: str = "",
    welcome_message: str = "",
    suggested_questions: Optional[List[str]] = None,
    bindings: Optional[Dict[str, Any]] = None,
    model_config: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """An admin directly "creates" a sub-agent and lists it on the marketplace as approved (no global agent created).

    Mirrors the skill side's "creating a skill lists it on the skill marketplace": the new
    sub-agent enters the sub-agent marketplace (explicitly installable as a global / private
    clone) and does not take effect globally right away. Content comes from form fields rather
    than a source ``UserAgent``, hence a synthetic placeholder ``agent_id`` (installs always use
    the snapshot and never look back at a source agent).
    """
    now = utc_now()
    sub = agent_submission_data.new_submission(
        submission_id=f"aadm_{uuid.uuid4().hex[:16]}",
        slug=agent_submission_data.derive_slug(db, name),
        agent_id=f"__market__{uuid.uuid4().hex[:12]}",
        owner_user_id=agents_state.ADMIN_UPLOAD_OWNER,
        submitter_name=submitter_name,
        name=name,
        avatar=avatar or "",
        description=description or "",
        summary=(summary or "").strip() or (description or ""),
        category=validate_category(category),
        note="管理员创建，直接上架",
        system_prompt=system_prompt or "",
        welcome_message=welcome_message or "",
        suggested_questions=suggested_questions,
        model_config_snapshot=model_config,
        bindings_snapshot=bindings,
        tags=tags,
        status="approved",
        reviewed_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info("agent_market_admin_created: slug=%s name=%s", sub.slug, name)
    return {"slug": sub.slug, "action": "created", "message": "子智能体已上架子智能体市场"}


def delete_market_agent(db: Session, slug: str) -> Dict[str, Any]:
    """An admin deletes one DB listing record from the marketplace. Presets ship with the repo and cannot be deleted online (400)."""
    if agent_catalog.read_bundle(slug) is not None:
        raise BadRequestError(message="预置子智能体随仓库发布，不可在线删除")
    sub = db.query(AgentMarketSubmission).filter(AgentMarketSubmission.slug == slug).first()
    if sub is None:
        raise ResourceNotFoundError("marketplace_agent", slug)
    db.delete(sub)
    db.commit()
    logger.info("agent_market_deleted: slug=%s", slug)
    return {"slug": slug, "deleted": True}


def set_market_agent_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """List/delist a marketplace sub-agent (controls whether it shows in the marketplace; installed clones are unaffected)."""
    return ml.set_listing_enabled(db, ml.KIND_AGENT, slug, enabled, updated_by=updated_by)
