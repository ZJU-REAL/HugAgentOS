"""Runtime catalog composition from static catalog.json plus DB capabilities.

``catalog.json`` is the repository-controlled default catalog. Admin-created
skills, marketplace/plugin skills, and admin MCP rows live in the database and
are merged at read time by this module; they must not be persisted back into
``catalog.json``.
"""

from __future__ import annotations

import copy
import logging
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Optional

from core.config.catalog import get_catalog
from core.config.catalog_common import _item
from core.config.catalog_loader import (
    DB_HIDDEN_SERVERS,
    DB_UMBRELLA_ID,
    _database_query_capability_available,
    skill_body_from_raw,
)
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


_DEFAULT_MCP_ICONS: Dict[str, str] = {
    "query_database": "/home/mcp/数据库.svg",
    "retrieve_dataset_content": "/home/mcp/知识.svg",
    "internet_search": "/home/mcp/互联网.svg",
    "ai_chain_information_mcp": "/home/mcp/产业链.svg",
    "generate_chart_tool": "/home/mcp/数据.svg",
    "report_export_mcp": "/home/mcp/报告.svg",
    "web_fetch": "/home/mcp/来源.svg",
}
_DATABASE_QUERY_ENABLED_CONFIG = "database_query.capability_enabled"
_RUNTIME_DB_CACHE_TTL = 30.0
_runtime_db_cache: Dict[bool, tuple[float, bool, List[Dict[str, Any]], List[Dict[str, Any]]]] = {}
_runtime_db_cache_lock = Lock()


def invalidate_runtime_catalog_cache() -> None:
    """Clear cached public DB capability overlays."""
    with _runtime_db_cache_lock:
        _runtime_db_cache.clear()


def _merge_items_by_id(
    base_items: List[Dict[str, Any]], db_items: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge DB items into a catalog bucket by id.

    Existing static ids keep their original position, but DB metadata/enabled
    state wins. New DB-only ids are appended in the order provided by callers.
    """
    merged: List[Dict[str, Any]] = [
        dict(item) for item in base_items if isinstance(item, dict) and item.get("id")
    ]
    index = {str(item.get("id")): i for i, item in enumerate(merged)}
    for item in db_items:
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            continue
        if item_id in index:
            merged[index[item_id]] = {**merged[index[item_id]], **item}
        else:
            index[item_id] = len(merged)
            merged.append(item)
    return merged


def _public_db_skill_items(db: Session, *, include_runtime_details: bool) -> List[Dict[str, Any]]:
    from core.db.models import AdminSkill

    icons: Dict[str, str] = {}
    if include_runtime_details:
        from core.services.skill_icon_service import get_skill_icons

        icons = get_skill_icons(db)
    rows = (
        db.query(AdminSkill)
        .filter(AdminSkill.owner_user_id.is_(None))
        .order_by(AdminSkill.skill_id)
        .all()
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        enabled = bool(row.is_enabled) and str(row.dep_status or "ready") == "ready"
        item = _item(
            item_id=row.skill_id,
            kind="tool_bundle",
            name=row.display_name or row.skill_id,
            description=row.description or "",
            enabled=enabled,
            version=row.version or "1.0.0",
            config={"tags": row.tags or []},
        )
        if include_runtime_details:
            detail = row.user_intro or skill_body_from_raw(row.skill_content or "")
            if detail:
                item["detail"] = detail
            icon = icons.get(row.skill_id, "")
            if icon:
                item["icon"] = icon
        items.append(item)
    return items


def _public_db_mcp_items(db: Session, *, include_runtime_details: bool) -> List[Dict[str, Any]]:
    from core.config.user_intros import MCP_SERVER_USER_INTROS
    from core.db.models import AdminMcpServer
    from core.services.mcp_service import is_removed_builtin_mcp_server

    rows = (
        db.query(AdminMcpServer)
        .filter(AdminMcpServer.owner_user_id.is_(None))
        .order_by(AdminMcpServer.sort_order, AdminMcpServer.server_id)
        .all()
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        if is_removed_builtin_mcp_server(
            row.server_id,
            source_plugin=row.source_plugin,
        ):
            continue
        if row.server_id in DB_HIDDEN_SERVERS:
            continue
        item = _item(
            item_id=row.server_id,
            kind="mcp_server",
            name=row.display_name or row.server_id,
            description=row.description or "",
            enabled=bool(row.is_enabled),
            version="1",
            config={"server": row.server_id},
            icon=row.icon or _DEFAULT_MCP_ICONS.get(row.server_id, ""),
        )
        if include_runtime_details:
            detail = row.user_intro or MCP_SERVER_USER_INTROS.get(row.server_id, "")
            if detail:
                item["detail"] = detail
        items.append(item)
    return items


def _config_bool(db: Session, key: str, default: bool) -> bool:
    try:
        from core.db.models import SystemConfig

        row = db.query(SystemConfig.config_value).filter(SystemConfig.config_key == key).first()
        if row is None or row[0] is None:
            return default
        return str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    except Exception as exc:  # noqa: BLE001
        logger.debug("runtime catalog config bool lookup failed for %s: %s", key, exc)
        return default


def _apply_database_query_state(catalog: Dict[str, Any], db: Session) -> None:
    """Apply DB-managed state for the static database-query umbrella item."""
    enabled = _config_bool(db, _DATABASE_QUERY_ENABLED_CONFIG, True)
    _set_database_query_state(catalog, enabled)


def _set_database_query_state(catalog: Dict[str, Any], enabled: bool) -> None:
    for item in catalog.get("mcp") or []:
        if not isinstance(item, dict) or item.get("id") != DB_UMBRELLA_ID:
            continue
        item["enabled"] = enabled
        return


def _public_db_overlay(
    db: Session, *, include_runtime_details: bool
) -> tuple[bool, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return cached public DB capability overlay.

    This overlay is deployment-global, so it can be reused across users. User
    overrides and private capabilities remain resolved in catalog_resolver.
    """
    now = monotonic()
    with _runtime_db_cache_lock:
        cached = _runtime_db_cache.get(include_runtime_details)
        if cached is not None:
            expires_at, db_query_enabled, skills, mcps = cached
            if now < expires_at:
                return db_query_enabled, copy.deepcopy(skills), copy.deepcopy(mcps)
            _runtime_db_cache.pop(include_runtime_details, None)

    db_query_enabled = _config_bool(db, _DATABASE_QUERY_ENABLED_CONFIG, True)
    try:
        skills = _public_db_skill_items(db, include_runtime_details=include_runtime_details)
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime catalog DB skill overlay failed: %s", exc)
        skills = []
    try:
        mcps = _public_db_mcp_items(db, include_runtime_details=include_runtime_details)
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime catalog DB MCP overlay failed: %s", exc)
        mcps = []

    with _runtime_db_cache_lock:
        _runtime_db_cache[include_runtime_details] = (
            now + _RUNTIME_DB_CACHE_TTL,
            db_query_enabled,
            copy.deepcopy(skills),
            copy.deepcopy(mcps),
        )
    return db_query_enabled, copy.deepcopy(skills), copy.deepcopy(mcps)


def warmup_runtime_catalog_cache() -> None:
    """Warm the public DB capability overlay used by chat capability resolution."""
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        get_runtime_catalog(db, include_runtime_details=False)


def get_runtime_catalog(
    db: Session,
    *,
    include_runtime_details: bool = True,
    base_catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return static catalog plus public DB-managed capabilities.

    Failures while reading DB capabilities degrade to the static catalog so a DB
    hiccup does not make the whole capability panel unusable.
    """
    catalog = copy.deepcopy(
        base_catalog
        if base_catalog is not None
        else get_catalog(include_runtime_details=include_runtime_details)
    )
    for key in ("skills", "agents", "mcp", "kb"):
        if not isinstance(catalog.get(key), list):
            catalog[key] = []

    # A deployment can retain old catalog data or an in-process cache from a
    # build that shipped database-query support.  The edition's runnable MCP
    # registry is authoritative: never surface the synthetic umbrella when its
    # implementation is absent.
    if not _database_query_capability_available():
        catalog["mcp"] = [
            item
            for item in catalog["mcp"]
            if not isinstance(item, dict) or item.get("id") != DB_UMBRELLA_ID
        ]

    try:
        db_query_enabled, db_skills, db_mcps = _public_db_overlay(
            db,
            include_runtime_details=include_runtime_details,
        )
        _set_database_query_state(catalog, db_query_enabled)
        catalog["skills"] = _merge_items_by_id(
            catalog.get("skills") or [],
            db_skills,
        )
        catalog["mcp"] = _merge_items_by_id(
            catalog.get("mcp") or [],
            db_mcps,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime catalog DB overlay merge failed: %s", exc)

    return catalog
