"""agent submission data responsibilities."""
from __future__ import annotations
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from features.marketplace.models import AgentMarketSubmission
from core.db.models import UserAgent
from core.infra.exceptions import ResourceNotFoundError
from core.infra.time import utc_now
from features.marketplace.agent_categories import DEFAULT_AGENT_CATEGORY
from sqlalchemy.orm import Session
from features.marketplace import agent_metadata
from features.marketplace import agents_state

def existing_slugs(db: Session) -> set:
    """Marketplace slugs currently taken: preset directory + all submissions (including rejected; slugs are table-wide unique)."""
    slugs = set()
    if agents_state.AGENT_MARKET_DIR.is_dir():
        slugs.update(c.name for c in agents_state.AGENT_MARKET_DIR.iterdir() if c.is_dir())
    slugs.update(r[0] for r in db.query(AgentMarketSubmission.slug).all())
    return slugs


def derive_slug(db: Session, name: str) -> str:
    """Derive the marketplace slug from the sub-agent name (ASCII-ized + unique); Chinese names fall back to agent-<hex>."""
    base = re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-")
    if not base:
        base = f"agent-{uuid.uuid4().hex[:8]}"
    taken = existing_slugs(db)
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def model_config_snapshot(agent: UserAgent) -> Dict[str, Any]:
    return {
        "temperature": float(agent.temperature) if agent.temperature is not None else None,
        "max_tokens": agent.max_tokens,
        "max_iters": agent.max_iters,
        "timeout": agent.timeout,
    }


def bindings_snapshot(agent: UserAgent) -> Dict[str, List[str]]:
    return {
        "skill_ids": list(agent.skill_ids or []),
        "mcp_server_ids": list(agent.mcp_server_ids or []),
        "plugin_ids": list(agent.plugin_ids or []),
        "kb_ids": list(agent.kb_ids or []),
    }


def submission_to_dict(
    sub: AgentMarketSubmission, *, with_content: bool = False
) -> Dict[str, Any]:
    data = {
        "submission_id": sub.submission_id,
        "slug": sub.slug,
        "agent_id": sub.agent_id,
        "owner_user_id": sub.owner_user_id,
        "submitter_name": sub.submitter_name or "",
        "name": sub.name,
        "avatar": sub.avatar or "",
        "description": sub.description or "",
        "summary": sub.summary or "",
        "category": sub.category or DEFAULT_AGENT_CATEGORY,
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "note": sub.note or "",
        "status": sub.status,
        "review_note": sub.review_note or "",
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }
    if with_content:
        data["system_prompt"] = sub.system_prompt or ""
        data["welcome_message"] = sub.welcome_message or ""
        data["suggested_questions"] = list(sub.suggested_questions or [])
        data["bindings"] = agent_metadata.normalize_binding_lists(sub.bindings_snapshot)
    return data


def owned_agent(db: Session, agent_id: str, owner_user_id: str) -> UserAgent:
    """Fetch the sub-agent owned by this user (owner_type=user), otherwise 404."""
    agent = (
        db.query(UserAgent)
        .filter(
            UserAgent.agent_id == agent_id,
            UserAgent.owner_type == "user",
            UserAgent.user_id == owner_user_id,
        )
        .first()
    )
    if agent is None:
        raise ResourceNotFoundError("user_agent", agent_id)
    return agent


def new_submission(
    *,
    submission_id: str,
    slug: str,
    agent_id: str,
    owner_user_id: str,
    name: str,
    category: str,
    status: str,
    submitter_name: str = "",
    avatar: Optional[str] = None,
    description: str = "",
    summary: str = "",
    note: str = "",
    system_prompt: str = "",
    welcome_message: str = "",
    suggested_questions: Optional[List[str]] = None,
    model_config_snapshot: Optional[Dict[str, Any]] = None,
    bindings_snapshot: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    reviewed_at: Optional[datetime] = None,
) -> AgentMarketSubmission:
    """Uniformly construct one ``AgentMarketSubmission`` (fixed fields and default normalization centralized here).

    The three entry points (user submission ``submit_to_marketplace`` / the create branch of the
    admin direct-upload ``publish_agent_to_market`` / admin create-and-list
    ``create_market_agent``) share the same field set; callers pass only what differs.
    """
    now = utc_now()
    return AgentMarketSubmission(
        submission_id=submission_id,
        slug=slug,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        submitter_name=(submitter_name or "").strip(),
        name=name,
        avatar=avatar,
        description=description or "",
        summary=summary or "",
        category=category,
        tags=list(tags or []),
        version="1.0.0",
        note=(note or "").strip(),
        system_prompt=system_prompt or "",
        welcome_message=welcome_message or "",
        suggested_questions=list(suggested_questions or []),
        model_config_snapshot=dict(model_config_snapshot or {}),
        bindings_snapshot=agent_metadata.normalize_binding_lists(bindings_snapshot),
        status=status,
        reviewed_at=reviewed_at,
        created_at=now,
        updated_at=now,
    )
