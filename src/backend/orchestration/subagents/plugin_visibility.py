"""Plugin visibility helpers shared by plan mode and tests."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _owner_visible(column, user_id: str):
    """Match rows owned by the user plus global rows with NULL owner."""
    return or_(column == user_id, column.is_(None))


def all_plugin_component_ids(db: Session, user_id: str) -> Tuple[set, set]:
    """Return plugin component ids visible to a user: (skill_ids, mcp_ids)."""
    from core.db.models import AdminMcpServer, AdminSkill

    skills: set = set()
    mcps: set = set()
    try:
        for (sid,) in (
            db.query(AdminSkill.skill_id)
            .filter(AdminSkill.source_plugin.isnot(None))
            .filter(_owner_visible(AdminSkill.owner_user_id, user_id))
            .all()
        ):
            if sid:
                skills.add(str(sid))
        for (mid,) in (
            db.query(AdminMcpServer.server_id)
            .filter(AdminMcpServer.source_plugin.isnot(None))
            .filter(_owner_visible(AdminMcpServer.owner_user_id, user_id))
            .all()
        ):
            if mid:
                mcps.add(str(mid))
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan: plugin component id load failed: %s", exc)
    return skills, mcps


def load_enabled_plugins(
    db: Session,
    user_id: str,
    enabled_plug_skills: set,
    enabled_plug_mcps: set,
) -> List[Dict[str, Any]]:
    """Load user-visible plugins with at least one enabled component."""
    from core.db.models import InstalledPlugin

    out: List[Dict[str, Any]] = []
    try:
        rows = (
            db.query(InstalledPlugin)
            .filter(_owner_visible(InstalledPlugin.owner_user_id, user_id))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan: installed plugin load failed: %s", exc)
        return out
    for r in rows:
        cids = r.component_ids or {}
        sk = [s for s in (cids.get("skills") or []) if s in enabled_plug_skills]
        mc = [m for m in (cids.get("mcp") or []) if m in enabled_plug_mcps]
        if not sk and not mc:
            continue
        out.append({
            "name": r.name or r.slug,
            "description": r.description or "",
            "skill_ids": sk,
            "mcp_ids": mc,
        })
    return out
