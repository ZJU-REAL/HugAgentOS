"""Runtime catalog composition from static catalog.json plus DB capabilities.

``catalog.json`` is the repository-controlled default catalog. Admin-created
skills, marketplace/plugin skills, and admin MCP rows live in the database and
are merged at read time by this module; they must not be persisted back into
``catalog.json``.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Optional

from core.capabilities.catalog.api import get_catalog
from core.capabilities.catalog.common import _item
from core.capabilities.catalog.details import resolve_skill_detail
from core.config.edition import edition_mcp_icons
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


DEFAULT_MCP_ICONS: Dict[str, str] = {
    "retrieve_dataset_content": "/home/mcp/knowledge.svg",
    "internet_search": "/home/mcp/internet.svg",
    "web_fetch": "/home/mcp/source.svg",
    **edition_mcp_icons(),
}
_RUNTIME_DB_CACHE_TTL = 30.0


def _capability_revision() -> Optional[str]:
    from core.db.capability_revision import current_revision

    return current_revision()


def invalidate_runtime_catalog_cache() -> None:
    """Clear cached public DB capability overlays."""
    with process_state().runtime_db_cache_lock:
        process_state().runtime_db_cache.clear()


def merge_items_by_id(
    base_items: List[Dict[str, Any]], db_items: List[Dict[str, Any]], key: str = "id"
) -> List[Dict[str, Any]]:
    """Merge DB items into a catalog bucket by id.

    Existing static ids keep their original position, but DB metadata/enabled
    state wins. New DB-only ids are appended in the order provided by callers.
    ``key`` names the identity field, for buckets keyed by something other than
    ``id`` (agents by ``agent_id``, plugins by ``slug`` — a plugin's install id
    differs between the device copy and the cloud copy, its slug does not).
    """
    merged: List[Dict[str, Any]] = [
        dict(item) for item in base_items if isinstance(item, dict) and item.get(key)
    ]
    index = {str(item.get(key)): i for i, item in enumerate(merged)}
    for item in db_items:
        item_id = str(item.get(key, "")).strip()
        if not item_id:
            continue
        if item_id in index:
            merged[index[item_id]] = {**merged[index[item_id]], **item}
        else:
            index[item_id] = len(merged)
            merged.append(dict(item))
    return merged


def _public_db_skill_items(db: Session, *, include_runtime_details: bool) -> List[Dict[str, Any]]:
    from core.db.models import AdminSkill

    icons: Dict[str, str] = {}
    if include_runtime_details:
        from core.capabilities.skills.icons import get_skill_icons

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
        item["created_at"] = row.created_at.isoformat() if row.created_at else None
        if include_runtime_details:
            detail = resolve_skill_detail(row.user_intro, row.skill_content or "")
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
    from core.capabilities.mcp.service import is_removed_builtin_mcp_server

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
        item = _item(
            item_id=row.server_id,
            kind="mcp_server",
            name=row.display_name or row.server_id,
            description=row.description or "",
            enabled=bool(row.is_enabled),
            version="1",
            config={"server": row.server_id},
            icon=row.icon or DEFAULT_MCP_ICONS.get(row.server_id, ""),
        )
        item["created_at"] = row.created_at.isoformat() if row.created_at else None
        # Feature that owns the row (e.g. "datasources"); clients key tool UI off it.
        from core.capabilities.mcp.service import mcp_owner

        owner = mcp_owner(row.extra_config)
        if owner:
            item["managed_by"] = owner
        item["tools"] = [
            t["name"]
            for t in (row.tools_json or [])
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
        if include_runtime_details:
            detail = row.user_intro or MCP_SERVER_USER_INTROS.get(row.server_id, "")
            if detail:
                item["detail"] = detail
        items.append(item)
    return items


def _public_db_overlay(
    db: Session, *, include_runtime_details: bool
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return cached public DB capability overlay.

    This overlay is deployment-global, so it can be reused across users. User
    overrides and private capabilities remain resolved in catalog_resolver.
    """
    now = monotonic()
    revision = _capability_revision()
    with process_state().runtime_db_cache_lock:
        cached = process_state().runtime_db_cache.get(include_runtime_details)
        if cached is not None:
            expires_at, cached_revision, skills, mcps = cached
            # Another worker's MCP or skill change shows here on the next lookup, not after the TTL.
            if now < expires_at and cached_revision == revision:
                return copy.deepcopy(skills), copy.deepcopy(mcps)
            process_state().runtime_db_cache.pop(include_runtime_details, None)

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

    with process_state().runtime_db_cache_lock:
        process_state().runtime_db_cache[include_runtime_details] = (
            now + _RUNTIME_DB_CACHE_TTL,
            revision,
            copy.deepcopy(skills),
            copy.deepcopy(mcps),
        )
    return copy.deepcopy(skills), copy.deepcopy(mcps)


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

    from core.harness.registry import device_catalog

    if device_catalog.active():
        # 混合模式：能力一律以云端账号同步下来的那份为准。本机自带的内置技能 / 连接器
        # 在这里就整体清空，清单、装配、显式点选、开关校验——所有读这份目录的地方同时
        # 生效，不会出现"能力中心看不到、模型还调得到"。用户自建的私有条目不走这条路，
        # 由 catalog_resolver / 路由按 owner 单独取，不受影响。
        return {**catalog, "skills": [], "mcp": []}

    try:
        db_skills, db_mcps = _public_db_overlay(
            db,
            include_runtime_details=include_runtime_details,
        )
        catalog["skills"] = merge_items_by_id(
            catalog.get("skills") or [],
            db_skills,
        )
        catalog["mcp"] = merge_items_by_id(
            catalog.get("mcp") or [],
            db_mcps,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime catalog DB overlay merge failed: %s", exc)

    return catalog


@dataclass
class RuntimeState:
    runtime_db_cache: Dict[bool, tuple[float, List[Dict[str, Any]], List[Dict[str, Any]]]] = field(default_factory=lambda: {})
    runtime_db_cache_lock: object = field(default_factory=lambda: Lock())


_STATE = RuntimeState()


def process_state() -> RuntimeState:
    return _STATE
