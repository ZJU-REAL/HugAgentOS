"""agent catalog responsibilities."""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from features.marketplace.models import AgentMarketSubmission
from core.infra.exceptions import ResourceNotFoundError
from features.marketplace import edition as ml
from features.marketplace.agent_categories import AGENT_MARKETPLACE_CATEGORIES
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.marketplace import agent_metadata
from features.marketplace import agents_state

def read_bundle(slug: str) -> Optional[Dict[str, Any]]:
    """Read and validate a single preset sub-agent's ``agent.json`` (fault-tolerant; returns None if broken)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    path = agents_state.AGENT_MARKET_DIR / slug / agents_state.AGENT_JSON
    if not path.is_file():
        return None
    try:
        b = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent market bundle broken: %s (%s)", slug, exc)
        return None
    if not isinstance(b, dict) or not b.get("name"):
        return None
    b.setdefault("slug", slug)
    return b


def approved_submissions(db: Session) -> List[AgentMarketSubmission]:
    return (
        db.query(AgentMarketSubmission)
        .filter(AgentMarketSubmission.status == "approved")
        .order_by(AgentMarketSubmission.created_at.desc())
        .all()
    )


def get_approved_submission(db: Session, slug: str) -> Optional[AgentMarketSubmission]:
    return (
        db.query(AgentMarketSubmission)
        .filter(
            AgentMarketSubmission.slug == slug,
            AgentMarketSubmission.status == "approved",
        )
        .first()
    )


def list_marketplace_agents(
    db: Session, *, include_disabled: bool = False, viewer_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List all marketplace sub-agents: presets (featured first) + community listings, layered with the enable/disable switch and visibility-scope filtering."""
    items: List[Dict[str, Any]] = []
    if agents_state.AGENT_MARKET_DIR.is_dir():
        for child in sorted(agents_state.AGENT_MARKET_DIR.iterdir()):
            if not child.is_dir():
                continue
            b = read_bundle(child.name)
            if b:
                items.append(agent_metadata.bundle_public_meta(b))
    items.sort(key=lambda x: (0 if x["featured"] else 1, x["slug"]))
    items.extend(agent_metadata.submission_public_meta(s) for s in approved_submissions(db))
    items = ml.annotate_and_filter(
        db,
        ml.KIND_AGENT,
        items,
        id_key="slug",
        include_disabled=include_disabled,
        viewer_user_id=viewer_user_id,
    )
    return items


def categories_from_items(items: List[Dict[str, Any]]) -> List[str]:
    """Derive categories from an already-fetched marketplace list: the fixed 9 major categories first, legacy leftover categories appended after."""
    seen: List[str] = list(AGENT_MARKETPLACE_CATEGORIES)
    for it in items:
        if it["category"] not in seen:
            seen.append(it["category"])
    return seen


def list_categories(
    db: Session, *, include_disabled: bool = False, viewer_user_id: Optional[str] = None
) -> List[str]:
    """Marketplace category list (standalone entry point; the list endpoint should reuse categories_from_items to avoid a second scan)."""
    return categories_from_items(
        list_marketplace_agents(
            db, include_disabled=include_disabled, viewer_user_id=viewer_user_id
        )
    )


def get_agent_detail(db: Session, slug: str) -> Dict[str, Any]:
    """Detail of a single marketplace sub-agent: metadata + prompt body + capability bindings (for the detail preview).

    The preset directory takes priority; falls back to the community listing (approved submission) if not found.
    """
    b = read_bundle(slug)
    if b:
        data = agent_metadata.bundle_public_meta(b)
        data["system_prompt"] = b.get("system_prompt") or ""
        data["welcome_message"] = b.get("welcome_message") or ""
        data["suggested_questions"] = list(b.get("suggested_questions") or [])
        data["bindings"] = agent_metadata.bindings_of(b)
        return data
    sub = get_approved_submission(db, slug)
    if sub is None:
        raise ResourceNotFoundError("marketplace_agent", slug)
    data = agent_metadata.submission_public_meta(sub)
    data["system_prompt"] = sub.system_prompt or ""
    data["welcome_message"] = sub.welcome_message or ""
    data["suggested_questions"] = list(sub.suggested_questions or [])
    data["bindings"] = agent_metadata.normalize_binding_lists(sub.bindings_snapshot)
    return data
