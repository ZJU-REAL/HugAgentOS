"""Catalog shape migration and item coercion helpers.

Handles legacy catalog.json formats and normalises items into the
canonical schema expected by the frontend.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.config.catalog_common import _item

_LOGGER = logging.getLogger(__name__)


def _ensure_array_node(data: Dict[str, Any], key: str) -> None:
    node = data.get(key)
    if not isinstance(node, list):
        data[key] = []


def _coerce_item(item: Dict[str, Any], kind_bucket: str) -> Dict[str, Any]:
    item_id = str(item.get("id", "")).strip()
    if not item_id:
        return {}
    name = str(item.get("name", item_id)).strip() or item_id
    description = str(item.get("description", item.get("desc", name))).strip() or name
    version = str(item.get("version", "v1")).strip() or "v1"
    config = item.get("config")
    detail = item.get("detail")
    icon = item.get("icon")
    item_kind = str(item.get("kind", "")).strip()
    if not item_kind:
        if kind_bucket == "agents":
            item_kind = "subagent"
        elif kind_bucket == "mcp":
            item_kind = "mcp_server"
        elif kind_bucket == "kb":
            item_kind = "knowledge_base"
        else:
            item_kind = "tool_bundle"
    return _item(
        item_id=item_id,
        kind=item_kind,
        name=name,
        description=description,
        enabled=bool(item.get("enabled")),
        version=version,
        config=config if isinstance(config, dict) else None,
        detail=detail if isinstance(detail, str) else None,
        icon=icon if isinstance(icon, str) and icon else None,
    )


def _migrate_legacy_shape(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate legacy catalog shapes to the current 4-bucket format.

    If the data already has {skills, agents, mcp, kb} it is normalised
    in-place.  Otherwise it is rebuilt from legacy key conventions.
    """
    from core.config.catalog_loader import _default_catalog

    # Already new shape.
    if all(k in raw for k in ("skills", "agents", "mcp", "kb")):
        out = dict(raw)
        for key in ("skills", "agents", "mcp", "kb"):
            _ensure_array_node(out, key)
            normalized: List[Dict[str, Any]] = []
            for x in out.get(key, []):
                if isinstance(x, dict):
                    item = _coerce_item(x, key)
                    if item:
                        normalized.append(item)
            out[key] = normalized
        return out

    # Legacy shape migration:
    # {
    #   "router_strategy": {...},
    #   "subagent": {...},
    #   "mcp_server": {...}
    # }
    out = _default_catalog()

    subagent = raw.get("subagent")
    if isinstance(subagent, dict):
        migrated_agents: List[Dict[str, Any]] = []
        for sid, sval in subagent.items():
            enabled = bool(sval.get("enabled")) if isinstance(sval, dict) else False
            migrated_agents.append(
                _item(
                    item_id=str(sid),
                    kind="subagent",
                    name=str(sid),
                    description=f"Legacy migrated subagent: {sid}",
                    enabled=enabled,
                )
            )
        if migrated_agents:
            out["agents"] = migrated_agents

    mcp = raw.get("mcp_server")
    if isinstance(mcp, dict):
        migrated_mcp: List[Dict[str, Any]] = []
        for mid, mval in mcp.items():
            enabled = bool(mval.get("enabled")) if isinstance(mval, dict) else False
            migrated_mcp.append(
                _item(
                    item_id=str(mid),
                    kind="mcp_server",
                    name=str(mid),
                    description=f"Legacy migrated mcp server: {mid}",
                    enabled=enabled,
                    config={"server": str(mid)},
                )
            )
        if migrated_mcp:
            out["mcp"] = migrated_mcp

    # Router strategy is intentionally no longer exposed in catalog.
    # Keep a migration trace in logs so legacy behavior changes are auditable.
    router = raw.get("router_strategy")
    if isinstance(router, dict):
        enabled = bool(router.get("enabled"))
        strategy = str(router.get("value", "")).strip() or str(router.get("strategy", "")).strip() or "unknown"
        _LOGGER.info(
            "catalog migration ignored legacy router_strategy (strategy=%s, enabled=%s); "
            "router is env-controlled via ROUTER_STRATEGY",
            strategy,
            enabled,
        )
    return out
