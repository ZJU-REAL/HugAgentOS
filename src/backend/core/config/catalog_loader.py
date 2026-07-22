"""Static catalog loading, caching, and explicit persistence.

Handles reading catalog.json from disk, building the default catalog
from repository built-ins, TTL-based in-memory caching, and explicit
admin writes. Database capabilities are merged at read time by
``catalog_runtime`` and are not written back to this file.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from time import monotonic
from typing import Any, Dict, List, Optional

from core.config.catalog_common import _CATALOG_PATH, _item, _read_raw_catalog

_LOGGER = logging.getLogger(__name__)

# "Database tools" unification: the three real DB MCP servers are hidden from the capability
# center / MCP tool list and represented by a single umbrella capability ``database_query``
# ("数据库查询"); agent_factory expands the umbrella alias into whichever server is actually
# enabled based on the data-source type.
DB_HIDDEN_SERVERS = {"query_database", "db_query", "es_query"}
DB_UMBRELLA_ID = "database_query"
DB_UMBRELLA_NAME = "数据库查询"
DB_UMBRELLA_DESC = (
    "统一的数据库查询能力。在 Config 后台「数据库工具」里配置数据源后，"
    "按所连数据库类型自动选择：自建智能取数走 query_database，直连 MySQL/PostgreSQL "
    "等走 db_query，Elasticsearch 走 es_query。"
)


def _database_query_capability_available() -> bool:
    """Whether this edition ships a runnable database-query implementation."""
    try:
        from mcp_servers._ports import PORTS

        return "query_database" in PORTS
    except Exception as exc:
        _LOGGER.warning("Database-query runtime registry unavailable: %s", exc)
        return False


def _private_skill_ids() -> set:
    """Set of private skill ids in admin_skills owned by some user (owner_user_id non-null).

    These skills don't enter the global catalog (injected per current user only by /v1/catalog),
    but the loader can still resolve / materialize / register them by id (owner verification
    happens at the request layer).
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        with SessionLocal() as db:
            return {
                row[0]
                for row in db.query(AdminSkill.skill_id)
                .filter(AdminSkill.owner_user_id.isnot(None))
                .all()
            }
    except Exception:
        return set()


# ── In-memory catalog cache (TTL-based) ────────────────────────────────────
_CATALOG_CACHE: Dict[bool, Dict[str, Any]] = {}  # key = include_runtime_details
_CATALOG_CACHE_TIME: Dict[bool, float] = {}
_CATALOG_CACHE_TTL: float = 10.0  # seconds


def invalidate_catalog_cache() -> None:
    """Clear the in-memory catalog cache (call after writes)."""
    _CATALOG_CACHE.clear()
    _CATALOG_CACHE_TIME.clear()


def _write_catalog(data: Dict[str, Any]) -> None:
    _CATALOG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    invalidate_catalog_cache()


# ── Default catalog construction ───────────────────────────────────────────


def _load_builtin_skill_metadata() -> List[Any]:
    """Load metadata from repository built-in skills only.

    The global multi-source loader also sees DB/admin/user/project skills. Those
    are runtime capabilities and must not become repository catalog defaults.
    """
    try:
        from core.agent_skills.backends.filesystem import FilesystemBackend
        from core.agent_skills.config import get_default_skill_sources
        from core.agent_skills.registry import _load_skill_metadata_from_file
    except Exception as e:
        _LOGGER.warning("Failed to import built-in skill loader: %s", e)
        return []

    builtin_source = next(
        (src for src in get_default_skill_sources() if src.name == "built-in"),
        None,
    )
    if builtin_source is None:
        return []

    backend = FilesystemBackend(
        root_dir=builtin_source.root_dir,
        source_name=builtin_source.name,
        priority=builtin_source.priority,
    )
    metadata: List[Any] = []
    for skill_info in backend.list_skill_files():
        try:
            metadata.append(_load_skill_metadata_from_file(skill_info.file_path))
        except Exception as e:
            _LOGGER.warning(
                "Failed to load built-in skill metadata from %s: %s", skill_info.file_path, e
            )
    return metadata


def _default_catalog() -> Dict[str, Any]:
    # Import lazily to avoid any startup surprises.
    try:
        from core.config.mcp_config import MCP_SERVERS

        mcp_servers = MCP_SERVERS
    except Exception as e:
        _LOGGER.warning(f"Failed to load MCP servers: {e}")
        mcp_servers = {}

    # Build MCP items from mcp_config.py with auto-extracted detail field
    try:
        from core.config.mcp_config import MCP_SERVER_DESCRIPTIONS as _MCP_ZH_DESC
        from core.config.mcp_config import MCP_SERVER_DISPLAY_NAMES as _MCP_ZH_NAMES
    except Exception:
        _MCP_ZH_NAMES = {}
        _MCP_ZH_DESC = {}

    mcp_items = [
        _item(
            item_id=k,
            kind="mcp_server",
            name=_MCP_ZH_NAMES.get(k, k),
            description=_MCP_ZH_DESC.get(k, f"MCP 服务：{_MCP_ZH_NAMES.get(k, k)}"),
            enabled=True,
            config={"server": k},
        )
        for k in mcp_servers.keys()
        if k not in DB_HIDDEN_SERVERS
    ]
    if _database_query_capability_available():
        mcp_items.append(
            _item(
                item_id=DB_UMBRELLA_ID,
                kind="mcp_server",
                name=DB_UMBRELLA_NAME,
                description=DB_UMBRELLA_DESC,
                enabled=True,
                config={"server": DB_UMBRELLA_ID},
            )
        )

    skill_items: List[Dict[str, Any]] = []
    for metadata in _load_builtin_skill_metadata():
        skill_items.append(
            _item(
                item_id=metadata.id,
                kind="tool_bundle",
                name=metadata.name,
                description=metadata.description,
                enabled=True,
                version=metadata.version,
                config={"tags": metadata.tags},
            )
        )
    if not skill_items:
        skill_items = [
            _item(
                item_id="report_generation_bundle",
                kind="tool_bundle",
                name="Report Generation Bundle",
                description="Builtin report generation capability bundle.",
                enabled=True,
                config={"bundle": "reporting"},
            )
        ]

    agent_items: List[Dict[str, Any]] = []

    return {
        "skills": skill_items,
        "agents": agent_items,
        "mcp": mcp_items,
        "kb": [],
    }


# ── Dynamic spec loading ──────────────────────────────────────────────────


def _extract_skill_file_path(skill_path: str) -> Path:
    raw = str(skill_path or "")
    actual_path = raw.split(":", 1)[1] if ":" in raw else raw
    return Path(actual_path)


def skill_body_from_raw(raw: str) -> str:
    """Take the "body" from the full SKILL.md text as the detail fallback (frontmatter stripped).

    Externally imported skills usually have no separately written "user intro"; rather than
    showing "no details", show the user the full SKILL.md body directly. name/description/tags/version
    from the frontmatter are already displayed separately in the card header, so only the body
    (the markdown after ``---``) is taken here, avoiding rendering raw YAML. Without valid
    frontmatter, fall back to the full text; on empty/exception fall back to empty string
    (preserving the original "no details" behavior).
    """
    if not raw:
        return ""
    try:
        from core.agent_skills.registry import _split_frontmatter

        try:
            _, body = _split_frontmatter(raw)
        except Exception:
            # Without valid frontmatter, use the full text directly
            return raw.strip()
        return (body or "").strip()
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("skill body fallback parse failed: %s", e)
        return ""


def _skill_body_fallback(loader, sid: str) -> str:
    """Read the full SKILL.md by skill_id and take the body (detail fallback). See skill_body_from_raw."""
    try:
        raw = loader._backend.read_skill_file(sid)
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("skill body fallback read failed for %s: %s", sid, e)
        return ""
    return skill_body_from_raw(raw)


def _load_dynamic_skill_specs() -> Dict[str, Dict[str, Any]]:
    """Load dynamic skill metadata + user-facing intro markdown.

    The ``detail`` field returned here powers the capability-center detail
    page. It is **not** the raw SKILL.md body — that content is for the agent.
    Priority chain:

      1. ``AdminSkill.user_intro``      (admin override, highest priority)
      2. ``SKILL_USER_INTROS[sid]``     (built-in default in user_intros.py)
      3. SKILL.md body fallback         (externally imported / no intro: show the full skill text directly)
      4. ``""``                         (when even SKILL.md is unavailable, frontend shows "暂无详情")
    """
    try:
        from core.agent_skills.loader import get_skill_loader

        loader = get_skill_loader()
        # Refresh metadata cache to support hot-reload for bind-mounted skill files.
        loader.clear_cache()
        metadata_map = loader.load_all_metadata()
    except Exception as e:
        _LOGGER.warning(f"Failed to load dynamic skill specs: {e}")
        return {}

    # Admin DB overrides for user_intro (skill_id → markdown) + skill icon mapping
    db_user_intros: Dict[str, str] = {}
    skill_icons: Dict[str, str] = {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.services.skill_icon_service import get_skill_icons

        with SessionLocal() as db:
            for sid, intro in db.query(AdminSkill.skill_id, AdminSkill.user_intro).all():
                if intro:
                    db_user_intros[sid] = intro
            skill_icons = get_skill_icons(db)
    except Exception as e:
        _LOGGER.debug("Could not load admin skill user_intros/icons from DB: %s", e)

    try:
        from core.config.user_intros import SKILL_USER_INTROS
    except Exception:
        SKILL_USER_INTROS = {}

    _private_ids = _private_skill_ids()
    result: Dict[str, Dict[str, Any]] = {}
    for sid, metadata in metadata_map.items():
        if sid in _private_ids:
            continue  # private skills don't enter the global catalog (injected per user by /v1/catalog)
        detail = db_user_intros.get(sid) or SKILL_USER_INTROS.get(sid, "")
        if not detail:
            detail = _skill_body_fallback(loader, sid)
        result[sid] = {
            "id": sid,
            "name": metadata.name,
            "description": metadata.description,
            "version": metadata.version,
            "tags": metadata.tags,
            "detail": detail,
            "icon": skill_icons.get(sid, ""),
        }
    return result


def get_skill_curated_detail(skill_id: str) -> Optional[Dict[str, Any]]:
    """Return a single skill's "capability-center display" detail, structured like one item of _load_dynamic_skill_specs.

    For reuse by SSE/tool cards, avoiding stuffing the full SKILL.md into the frontend. Returns None if not found.
    """
    sid = (skill_id or "").strip()
    if not sid:
        return None
    try:
        specs = _load_dynamic_skill_specs()
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("get_skill_curated_detail: load failed: %s", e)
        return None
    spec = specs.get(sid)
    if spec:
        return dict(spec)
    # Not in the global catalog → most likely a private / marketplace-installed skill (excluded
    # by _private_skill_ids). Resolve metadata by id directly + detail fallback, so the
    # load_skill tool card can still display properly.
    return _curated_detail_for_owned(sid)


def _curated_detail_for_owned(sid: str) -> Optional[Dict[str, Any]]:
    """Build a "capability-center display" detail for a private/marketplace skill (bypassing the global catalog).

    detail prefers AdminSkill.user_intro, otherwise falls back to the SKILL.md body. Returns None if the skill is not found.
    """
    try:
        from core.agent_skills.loader import get_skill_loader

        loader = get_skill_loader()
        metadata = loader.load_all_metadata().get(sid)
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("_curated_detail_for_owned: metadata load failed for %s: %s", sid, e)
        return None
    if not metadata:
        return None

    user_intro = ""
    icon = ""
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.services.skill_icon_service import get_skill_icons

        with SessionLocal() as db:
            row = db.query(AdminSkill.user_intro).filter(AdminSkill.skill_id == sid).first()
            if row and row[0]:
                user_intro = row[0]
            icon = get_skill_icons(db).get(sid, "")
    except Exception as e:  # pragma: no cover - defensive
        _LOGGER.debug("_curated_detail_for_owned: DB lookup failed for %s: %s", sid, e)

    detail = user_intro or _skill_body_fallback(loader, sid)
    return {
        "id": sid,
        "name": metadata.name,
        "description": metadata.description,
        "version": metadata.version,
        "tags": metadata.tags,
        "detail": detail,
        "icon": icon,
    }


def _load_dynamic_mcp_specs() -> Dict[str, Dict[str, str]]:
    """Load dynamic MCP specs (names + user-facing intro).

    The ``detail`` field is **not** the raw server.py docstring listing — that
    is developer-facing. ``detail`` is the user_intro markdown shown in the
    capability-center detail page. Priority:

      1. ``AdminMcpServer.user_intro``       (admin override, highest)
      2. ``MCP_SERVER_USER_INTROS[sid]``     (built-in default)
      3. ``""``                              (frontend shows "暂无介绍")
    """
    try:
        from core.config.mcp_config import (
            MCP_SERVER_DESCRIPTIONS,
            MCP_SERVER_DISPLAY_NAMES,
            MCP_SERVERS,
        )
    except Exception as e:
        _LOGGER.warning(f"Failed to load MCP configs: {e}")
        return {}

    try:
        from core.config.user_intros import MCP_SERVER_USER_INTROS
    except Exception:
        MCP_SERVER_USER_INTROS = {}

    # Admin DB user_intro overrides apply regardless of is_enabled — even a
    # temporarily disabled server should still display the admin-curated
    # intro when re-enabled or browsed.
    db_user_intros: Dict[str, str] = {}
    # Enabled admin-only rows (i.e. server_ids NOT in MCP_SERVERS) get appended
    # for runtime detail lookups. They are not persisted into catalog.json.
    enabled_admin_only_rows: list = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminMcpServer

        with SessionLocal() as db:
            # Only look at public MCPs (owner_user_id null). User-private MCPs don't enter the
            # global catalog; the /v1/catalog route injects them separately per current user.
            for row in (
                db.query(AdminMcpServer).filter(AdminMcpServer.owner_user_id.is_(None)).all()
            ):
                if row.user_intro:
                    db_user_intros[row.server_id] = row.user_intro
                if row.is_enabled and row.server_id not in MCP_SERVERS:
                    enabled_admin_only_rows.append(row)
    except Exception as e:
        _LOGGER.debug("Could not load admin MCP rows from DB: %s", e)

    result: Dict[str, Dict[str, str]] = {}
    for sid in MCP_SERVERS.keys():
        if sid in DB_HIDDEN_SERVERS:
            continue
        result[sid] = {
            "id": sid,
            "name": MCP_SERVER_DISPLAY_NAMES.get(sid, sid),
            "description": MCP_SERVER_DESCRIPTIONS.get(sid, f"MCP 服务：{sid}"),
            "detail": db_user_intros.get(sid) or MCP_SERVER_USER_INTROS.get(sid, ""),
        }

    # Append enabled admin-only MCP servers (those whose server_id isn't in the
    # static MCP_SERVERS dict) for runtime detail lookups only.
    for row in enabled_admin_only_rows:
        sid = row.server_id
        if sid in DB_HIDDEN_SERVERS:
            continue
        result[sid] = {
            "id": sid,
            "name": row.display_name or sid,
            "description": row.description or f"MCP 服务：{sid}",
            "detail": row.user_intro or MCP_SERVER_USER_INTROS.get(sid, ""),
        }

    # Inject the synthetic umbrella only when this edition ships a database
    # query runtime.  Otherwise the sync layer would recreate an item that the
    # CE build deliberately removed from catalog.json.
    if _database_query_capability_available():
        result[DB_UMBRELLA_ID] = {
            "id": DB_UMBRELLA_ID,
            "name": DB_UMBRELLA_NAME,
            "description": DB_UMBRELLA_DESC,
            "detail": MCP_SERVER_USER_INTROS.get(DB_UMBRELLA_ID, ""),
        }

    return result


# ── Sync & attach ─────────────────────────────────────────────────────────


def _sync_catalog_items_from_sources(data: Dict[str, Any]) -> bool:
    """Ensure catalog has all skills/MCP ids discovered from dynamic sources."""
    changed = False

    skills_node = data.get("skills")
    if not isinstance(skills_node, list):
        skills_node = []
        data["skills"] = skills_node
        changed = True
    mcp_node = data.get("mcp")
    if not isinstance(mcp_node, list):
        mcp_node = []
        data["mcp"] = mcp_node
        changed = True

    skill_specs = _load_dynamic_skill_specs()
    skill_index = {
        str(item.get("id", "")).strip(): item for item in skills_node if isinstance(item, dict)
    }
    for sid, spec in skill_specs.items():
        if sid in skill_index:
            item = skill_index[sid]
            # Always sync name/description/version from dynamic source so that
            # admin edits (e.g. display_name changes) are reflected immediately.
            if str(item.get("name", "")).strip() != spec["name"]:
                item["name"] = spec["name"]
                changed = True
            if str(item.get("description", "")).strip() != spec["description"]:
                item["description"] = spec["description"]
                item["desc"] = spec["description"]
                changed = True
            if str(item.get("version", "")).strip() != spec["version"]:
                item["version"] = spec["version"]
                changed = True
            if not isinstance(item.get("config"), dict):
                item["config"] = {}
                changed = True
            if spec["tags"] and not item["config"].get("tags"):
                item["config"]["tags"] = spec["tags"]
                changed = True
            continue

        skills_node.append(
            _item(
                item_id=sid,
                kind="tool_bundle",
                name=spec["name"],
                description=spec["description"],
                enabled=True,
                version=spec["version"],
                config={"tags": spec["tags"]},
            )
        )
        changed = True

    # Remove stale skill entries whose id no longer exists in any dynamic source
    before = len(skills_node)
    skills_node[:] = [
        item
        for item in skills_node
        if isinstance(item, dict) and str(item.get("id", "")).strip() in skill_specs
    ]
    if len(skills_node) != before:
        changed = True

    mcp_specs = _load_dynamic_mcp_specs()
    mcp_index = {
        str(item.get("id", "")).strip(): item for item in mcp_node if isinstance(item, dict)
    }
    for sid, spec in mcp_specs.items():
        if sid in mcp_index:
            item = mcp_index[sid]
            if not str(item.get("name", "")).strip():
                item["name"] = spec["name"]
                changed = True
            if not str(item.get("description", "")).strip():
                item["description"] = spec["description"]
                item["desc"] = spec["description"]
                changed = True
            # Legacy-data compatibility: early on "v1" was written into catalog.json as the
            # version, and the frontend adds another "v" prefix making "vv1". Normalize to a bare version number.
            ver = str(item.get("version", "")).strip()
            if ver and ver[:1].lower() == "v" and ver[1:2].isdigit():
                item["version"] = ver[1:]
                changed = True
            cfg = item.get("config")
            if not isinstance(cfg, dict):
                item["config"] = {"server": sid}
                changed = True
            elif not str(cfg.get("server", "")).strip():
                cfg["server"] = sid
                changed = True
            continue

        mcp_node.append(
            _item(
                item_id=sid,
                kind="mcp_server",
                name=spec["name"],
                description=spec["description"],
                enabled=True,
                config={"server": sid},
            )
        )
        changed = True

    # Remove stale MCP entries whose id no longer exists in any dynamic source
    before = len(mcp_node)
    mcp_node[:] = [
        item
        for item in mcp_node
        if isinstance(item, dict) and str(item.get("id", "")).strip() in mcp_specs
    ]
    if len(mcp_node) != before:
        changed = True

    return changed


def _strip_static_detail_fields(data: Dict[str, Any]) -> bool:
    """Remove persisted detail fields for dynamic-detail kinds (skills/mcp)."""
    changed = False
    for key in ("skills", "mcp"):
        node = data.get(key)
        if not isinstance(node, list):
            continue
        for item in node:
            if isinstance(item, dict) and "detail" in item:
                item.pop("detail", None)
                changed = True
    return changed


def _filter_to_static_defaults(data: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """Keep only repository default item ids in static catalog buckets.

    Older deployments may have a persisted ``CATALOG_PATH`` file that was
    materialized from DB/admin/plugin sources. Runtime reads should ignore
    those historical rows without rewriting the file.
    """
    for key in ("skills", "agents", "mcp", "kb"):
        default_items = [it for it in defaults.get(key, []) if isinstance(it, dict)]
        default_by_id = {
            str(it.get("id", "")).strip(): copy.deepcopy(it)
            for it in default_items
            if str(it.get("id", "")).strip()
        }
        if not default_by_id:
            data[key] = []
            continue

        source_items = data.get(key)
        if not isinstance(source_items, list):
            source_items = []

        filtered: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw_item in source_items:
            if not isinstance(raw_item, dict):
                continue
            item_id = str(raw_item.get("id", "")).strip()
            if not item_id or item_id in seen or item_id not in default_by_id:
                continue
            filtered.append({**copy.deepcopy(default_by_id[item_id]), **raw_item})
            seen.add(item_id)

        for item in default_items:
            item_id = str(item.get("id", "")).strip()
            if item_id and item_id not in seen:
                filtered.append(copy.deepcopy(item))

        data[key] = filtered


def _attach_runtime_details(data: Dict[str, Any]) -> None:
    """Attach dynamic runtime details for skills and mcp without persisting."""
    skill_specs = _load_dynamic_skill_specs()
    skills_node = data.get("skills")
    if isinstance(skills_node, list):
        for item in skills_node:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            spec = skill_specs.get(sid)
            if not spec:
                continue
            # Always sync name/description/version from dynamic source
            item["name"] = spec["name"]
            if spec["detail"]:
                item["detail"] = spec["detail"]
            if spec.get("icon"):
                item["icon"] = spec["icon"]
            item["description"] = spec["description"]
            item["desc"] = spec["description"]
            item["version"] = spec["version"]

    mcp_specs = _load_dynamic_mcp_specs()
    mcp_node = data.get("mcp")
    if isinstance(mcp_node, list):
        for item in mcp_node:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            spec = mcp_specs.get(sid)
            if not spec:
                continue
            # Preserve explicit catalog.json labels/descriptions so manual edits
            # remain visible in the frontend. Runtime MCP metadata still fills
            # blanks and provides the dynamic detail block below.
            if not str(item.get("name", "")).strip():
                item["name"] = spec["name"]
            if not str(item.get("description", "")).strip():
                item["description"] = spec["description"]
            if not str(item.get("desc", "")).strip():
                item["desc"] = str(item.get("description") or spec["description"])
            if spec["detail"]:
                item["detail"] = spec["detail"]
            cfg = item.get("config")
            if not isinstance(cfg, dict):
                item["config"] = {"server": sid}
            elif not str(cfg.get("server", "")).strip():
                cfg["server"] = sid


# ── Full catalog load (with cache) ────────────────────────────────────────


def ensure_default_catalog() -> Dict[str, Any]:
    """Create catalog.json with repository defaults if missing; return loaded catalog."""
    if not _CATALOG_PATH.exists():
        cat = _default_catalog()
        _CATALOG_PATH.write_text(
            json.dumps(cat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        invalidate_catalog_cache()
        return cat

    return load_catalog()


def load_catalog(*, include_runtime_details: bool = True) -> Dict[str, Any]:
    """Load catalog.json; if missing or invalid, recreate defaults.

    Results are cached in-memory for up to ``_CATALOG_CACHE_TTL`` seconds to
    avoid repeated disk I/O and dynamic source loading on every request.
    """
    from core.config.catalog_migration import _migrate_legacy_shape

    now = monotonic()
    cached_time = _CATALOG_CACHE_TIME.get(include_runtime_details, 0.0)
    if include_runtime_details in _CATALOG_CACHE and (now - cached_time) < _CATALOG_CACHE_TTL:
        return copy.deepcopy(_CATALOG_CACHE[include_runtime_details])

    if not _CATALOG_PATH.exists():
        result = _default_catalog()
        _CATALOG_CACHE[include_runtime_details] = copy.deepcopy(result)
        _CATALOG_CACHE_TIME[include_runtime_details] = monotonic()
        return result

    try:
        raw = _read_raw_catalog()
        data = _migrate_legacy_shape(raw)
    except Exception:
        # Return defaults on any parse/shape error without mutating repo files.
        data = _default_catalog()
        return data

    # Ensure required top-level keys exist and keep arrays.
    defaults = _default_catalog()
    for key in ("skills", "agents", "mcp", "kb"):
        if key not in data:
            data[key] = defaults[key]
        if not isinstance(data.get(key), list):
            data[key] = []
    _filter_to_static_defaults(data, defaults)

    # Do not persist static detail fields for skills/mcp.  This cleanup is kept
    # in-memory here; explicit admin operations remain the only catalog writers.
    _strip_static_detail_fields(data)

    if include_runtime_details:
        _attach_runtime_details(data)

    _CATALOG_CACHE[include_runtime_details] = copy.deepcopy(data)
    _CATALOG_CACHE_TIME[include_runtime_details] = monotonic()
    return data
