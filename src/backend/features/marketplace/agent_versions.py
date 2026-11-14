"""Version-aware sources for the existing agent marketplace API."""

from __future__ import annotations
import features.marketplace.agent_catalog as market_owner_agent_catalog
import features.marketplace.agent_installation as market_owner_agent_installation
from features.marketplace.agents_state import MAX_DESC as _MAX_DESC
import logging
logger = logging.getLogger(__name__)

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session


import features.marketplace.versions.store as store






def get_agent_detail(db, slug):
    native = market_owner_agent_catalog.get_agent_detail(db, slug)
    return store.detail(db, "agent", slug, native)


def _resolve_market_entry(db, slug):
    native = market_owner_agent_installation.resolve_market_entry(db, slug)
    snapshot = store.active(db, "agent", slug)
    return json.loads(store.decode(snapshot)["agent.json"]) if snapshot else native


def install_marketplace_agent(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    operator_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Install a marketplace sub-agent as a private clone (admin owner_user_id=None → global admin sub-agent)."""
    from core.capabilities.agents.edition import UserAgentService

    entry = _resolve_market_entry(db, slug)
    from features.marketplace import skill_versions

    final_bindings, report = market_owner_agent_installation.resolve_bindings(
        db, entry["bindings"], owner_user_id, operator_name,
        install_skill=skill_versions.install_marketplace_skill,
    )

    mc = entry.get("model_config") or {}
    data: Dict[str, Any] = {
        "name": entry["name"],
        "avatar": entry.get("avatar") or None,
        "description": (entry.get("summary") or entry.get("description") or "")[:_MAX_DESC],
        "system_prompt": entry.get("system_prompt") or "",
        "welcome_message": entry.get("welcome_message") or "",
        "suggested_questions": list(entry.get("suggested_questions") or []),
        "skill_ids": final_bindings["skill_ids"],
        "mcp_server_ids": final_bindings["mcp_server_ids"],
        "plugin_ids": final_bindings["plugin_ids"],
        "kb_ids": final_bindings["kb_ids"],
        "source_market_slug": slug,
        "ontology_tags": list(entry.get("ontology_tags") or []),
    }
    if mc.get("temperature") is not None:
        data["temperature"] = mc["temperature"]
    if mc.get("max_tokens") is not None:
        data["max_tokens"] = mc["max_tokens"]
    if mc.get("max_iters") is not None:
        data["max_iters"] = mc["max_iters"]
    if mc.get("timeout") is not None:
        data["timeout"] = mc["timeout"]

    owner_type = "admin" if owner_user_id is None else "user"
    agent = UserAgentService(db).create(
        user_id=owner_user_id,
        operator_name=operator_name,
        owner_type=owner_type,
        data=data,
    )
    logger.info(
        "agent_market_install: slug=%s owner=%s agent=%s bound=%d installed=%d dropped=%d",
        slug,
        owner_user_id or "global",
        agent["agent_id"],
        len(report["bound"]),
        len(report["installed"]),
        len(report["dropped"]),
    )
    return {
        "agent_id": agent["agent_id"],
        "slug": slug,
        "owner": "self" if owner_user_id else "global",
        "install_report": report,
        "message": "子智能体已安装",
    }
