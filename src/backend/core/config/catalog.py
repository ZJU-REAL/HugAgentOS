"""Project-local capability catalog.

Frontend contract (hugagent-ui-react) expects:
{
  "skills": [...],
  "agents": [...],
  "mcp": [...],
  "kb": [...]
}

Each item follows a minimal auditable schema:
- id
- kind (router|subagent|mcp_server|tool_bundle|knowledge_base)
- name
- description
- desc (alias for frontend display)
- enabled
- version
- config (optional object)

This module is the public API surface.  Internal loading / caching /
migration logic lives in ``catalog_loader`` and ``catalog_migration``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.config.catalog_common import _item, _normalize_kind
from core.config.catalog_loader import (
    _write_catalog,
    ensure_default_catalog,
    invalidate_catalog_cache,
    load_catalog,
)

# Re-export so existing ``from core.config.catalog import invalidate_catalog_cache``
# and ``from core.config.catalog import ensure_default_catalog`` keep working.
__all__ = [
    "get_catalog",
    "is_enabled",
    "get_enabled_ids",
    "set_enabled",
    "reorder_items",
    "invalidate_catalog_cache",
    "ensure_default_catalog",
]


# ── Public API ─────────────────────────────────────────────────────────────

def get_catalog(*, include_runtime_details: bool = True) -> Dict[str, Any]:
    """Load catalog.json; if missing or invalid, recreate defaults.

    Results are cached in-memory with a short TTL to avoid repeated
    disk I/O and dynamic source loading on every request.
    """
    return load_catalog(include_runtime_details=include_runtime_details)


def is_enabled(kind: str, item_id: str) -> bool:
    kind = _normalize_kind(kind)
    if kind not in {"skills", "agents", "mcp", "kb"}:
        return False

    cat = get_catalog(include_runtime_details=False)
    node = cat.get(kind)
    if not isinstance(node, list):
        return False
    for item in node:
        if isinstance(item, dict) and str(item.get("id")) == item_id:
            return bool(item.get("enabled"))
    return False


def get_enabled_ids(kind: str) -> List[str]:
    kind = _normalize_kind(kind)
    cat = get_catalog(include_runtime_details=False)
    node = cat.get(kind)
    if not isinstance(node, list):
        return []
    out: List[str] = []
    for item in node:
        if isinstance(item, dict) and item.get("enabled"):
            item_id = str(item.get("id", "")).strip()
            if item_id:
                out.append(item_id)
    return out


def _default_item_for_kind(kind: str, item_id: str, enabled: bool) -> Dict[str, Any]:
    item_kind = "tool_bundle"
    if kind == "agents":
        item_kind = "subagent"
    elif kind == "mcp":
        item_kind = "mcp_server"
    elif kind == "kb":
        item_kind = "knowledge_base"

    return _item(
        item_id=item_id,
        kind=item_kind,
        name=item_id,
        description=f"Catalog item: {item_id}",
        enabled=enabled,
    )


def set_enabled(kind: str, item_id: str, enabled: bool) -> Dict[str, Any]:
    """Set enabled flag for (kind, id) and persist to catalog.json."""
    kind = _normalize_kind(kind)
    if kind not in {"skills", "agents", "mcp", "kb"}:
        raise ValueError(f"unsupported kind: {kind}")

    cat = get_catalog(include_runtime_details=False)
    node = cat.get(kind)
    if not isinstance(node, list):
        node = []
        cat[kind] = node

    target: Dict[str, Any] | None = None
    for x in node:
        if isinstance(x, dict) and str(x.get("id")) == item_id:
            target = x
            break

    if target is None:
        target = _default_item_for_kind(kind, item_id, bool(enabled))
        node.append(target)
    else:
        target["enabled"] = bool(enabled)

    _write_catalog(cat)
    return {"item": target, "catalog": cat}


def reorder_items(kind: str, ordered_ids: List[str]) -> Dict[str, Any]:
    """Reorder catalog items of ``kind`` to match ``ordered_ids``.

    Ids listed in ``ordered_ids`` are placed first in the given order; any
    catalog items not mentioned keep their relative order at the end. The
    array order in catalog.json is the display order, so this is what the
    user-facing skill library renders.
    """
    kind = _normalize_kind(kind)
    if kind not in {"skills", "agents", "mcp", "kb"}:
        raise ValueError(f"unsupported kind: {kind}")

    cat = get_catalog(include_runtime_details=False)
    node = cat.get(kind)
    if not isinstance(node, list):
        return {"catalog": cat}

    by_id: Dict[str, Dict[str, Any]] = {
        str(x.get("id")): x for x in node if isinstance(x, dict)
    }
    new_node: List[Dict[str, Any]] = []
    seen: set = set()
    for raw_id in ordered_ids:
        item_id = str(raw_id)
        item = by_id.get(item_id)
        if item is not None and item_id not in seen:
            new_node.append(item)
            seen.add(item_id)
    for x in node:
        if isinstance(x, dict) and str(x.get("id")) not in seen:
            new_node.append(x)

    cat[kind] = new_node
    _write_catalog(cat)
    return {"catalog": cat}
