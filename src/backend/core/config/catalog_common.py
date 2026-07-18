"""Shared helpers used by catalog_loader and catalog_migration.

Kept in a separate module to avoid circular imports between the two.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

_CATALOG_PATH = Path(os.environ.get("CATALOG_PATH", str(Path(__file__).with_name("catalog.json"))))
_LOGGER = logging.getLogger(__name__)


def _normalize_kind(kind: str) -> str:
    raw = (kind or "").strip().lower()
    alias = {
        "skills": "skills",
        "skill": "skills",
        "agents": "agents",
        "agent": "agents",
        "subagent": "agents",
        "subagents": "agents",
        "mcp": "mcp",
        "mcp_server": "mcp",
        "mcp_servers": "mcp",
        "kb": "kb",
        "knowledge_base": "kb",
    }
    return alias.get(raw, raw)


def _item(
    *,
    item_id: str,
    kind: str,
    name: str,
    description: str,
    enabled: bool,
    version: str = "1",
    config: Optional[Dict[str, Any]] = None,
    detail: Optional[str] = None,
    icon: Optional[str] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": item_id,
        "kind": kind,
        "name": name,
        "description": description,
        # `desc` is kept for compatibility with hugagent-ui-react type expectations.
        "desc": description,
        "enabled": bool(enabled),
        "version": version,
    }
    if isinstance(config, dict) and config:
        data["config"] = config
    if detail:
        data["detail"] = detail
    if icon:
        data["icon"] = icon
    return data


def _read_raw_catalog() -> Dict[str, Any]:
    raw = _CATALOG_PATH.read_text(encoding="utf-8")
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("catalog must be a JSON object")
    return data
