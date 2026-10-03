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
from time import monotonic
from typing import Any, Dict, List

from core.config.catalog_common import _CATALOG_PATH, _item, _read_raw_catalog

_LOGGER = logging.getLogger(__name__)

# "Database tools" unification: the three real DB MCP servers are hidden from the capability
# center / MCP tool list and represented by a single umbrella capability ``database_query``
# ("数据库查询"); agent_factory expands the umbrella alias into whichever server is actually
# enabled based on the data-source type.
DB_HIDDEN_SERVERS = {"query_database", "db_query", "es_query"}
DB_UMBRELLA_ID = "database_query"
DB_UMBRELLA_NAME = "数据库查询"
# 伞形条目没有对应的 AdminMcpServer 行，图标取不到库里——定义在这里，云端目录与
# 桌面端投影共用同一个值，两边才不会一边有图标一边没有。
DB_UMBRELLA_ICON = "/home/mcp/database.svg"
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


# ── Sync & attach ─────────────────────────────────────────────────────────


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
        from core.config.catalog_details import _attach_runtime_details

        _attach_runtime_details(data)

    _CATALOG_CACHE[include_runtime_details] = copy.deepcopy(data)
    _CATALOG_CACHE_TIME[include_runtime_details] = monotonic()
    return data
