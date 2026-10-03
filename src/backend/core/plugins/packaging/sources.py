"""Plugin sources responsibilities."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugins.packaging import definitions as plugin_definitions
from core.plugins.packaging.importer import _ext_or_top, manifest_extensions
from core.services.marketplace_service import compute_install_id

logger = logging.getLogger(__name__)


def _iter_plugin_dirs():
    """Iterate over all plugin bundle directories containing plugin.json under default + marketplace."""
    for root in plugin_definitions.PLUGIN_SOURCE_DIRS:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "plugin.json").is_file():
                yield child


def _resolve_plugin_dir(slug: str) -> Optional[Path]:
    """Locate a plugin bundle directory by slug in default / marketplace (default takes precedence)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    for root in plugin_definitions.PLUGIN_SOURCE_DIRS:
        d = root / slug
        if d.is_dir() and (d / "plugin.json").is_file():
            return d
    return None


def _make_plugin_install_id(slug: str, owner_user_id: Optional[str]) -> str:
    return f"{slug}@{owner_user_id or 'global'}"


def _sanitize_id(value: str, maxlen: int) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-")
    return (s or "x")[:maxlen]


def _sanitize_component_id(value: str, maxlen: int) -> str:
    """Sanitize a namespaced component id without creating truncation collisions."""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-") or "x"
    if len(normalized) <= maxlen:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    prefix = normalized[: maxlen - len(digest) - 1].rstrip("-")
    return f"{prefix}-{digest}"


def _make_skill_id(slug: str, skill_name: str, owner_user_id: Optional[str]) -> str:
    """Namespaced skill id: {slug}-{skill} (+ user fingerprint). Constrained by _ID_RE (<=63)."""
    base = _sanitize_component_id(f"{slug}-{skill_name}", 50)
    return compute_install_id(
        base, owner_user_id
    )  # appends -<6-char fingerprint> when owner is non-empty


def _make_server_id(slug: str, server_name: str, owner_user_id: Optional[str]) -> str:
    """Namespaced MCP server id: {slug}-{server} (+ user fingerprint), same rules as skill ids."""
    return compute_install_id(
        _sanitize_component_id(f"{slug}-{server_name}", 80),
        owner_user_id,
    )


def builtin_plugin_component_ids() -> Tuple[set, set]:
    """Component ids provided by builtin plugin bundles (``plugin_bundles/{default,marketplace}``).

    Returns ``(skill_ids, mcp_ids)``, derived by scanning each bundle's
    ``skills/*/`` directories and its MCP declarations (standard ``mcp.json`` /
    legacy ``.mcp.json`` / manifest-inline) — the Agent Plugins standard
    manifest carries no ``components`` list, the filesystem is the truth. Even
    when **not installed**, these components already bubble up as first-class
    entries via static paths (e.g. MCP via ``_ports.py`` → catalog.json), so
    this is used to remove them from the "skill library / MCP tool library" and
    show them only under "Plugins", complementing the DB installation source
    (``AdminMcpServer.source_plugin`` non-empty). Pure filesystem scan, no DB
    dependency.
    """
    import json

    from core.plugins.packaging.importer import _find_mcp_map

    skill_ids: set = set()
    mcp_ids: set = set()
    for child in _iter_plugin_dirs():
        skills_root = child / "skills"
        if skills_root.is_dir():
            for c in skills_root.iterdir():
                if c.is_dir() and (c / "SKILL.md").is_file():
                    skill_ids.add(c.name)
        try:
            m = json.loads((child / "plugin.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            m = {}
        try:
            mcp_ids.update(_find_mcp_map(child, m if isinstance(m, dict) else {}).keys())
        except Exception:  # noqa: BLE001
            pass
    return skill_ids, mcp_ids


def _scan_native_manifest(plugin_dir: Path) -> Optional[Dict[str, Any]]:
    """Lightweight read of a builtin plugin bundle's metadata (no full normalize).

    Standard manifests carry no display fields — display metadata is overlaid
    later from resolve_market_meta; platform fields are read from the extension
    namespace (legacy top-level as fallback).
    """
    import json

    mp = plugin_dir / "plugin.json"
    if not mp.is_file():
        return None
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(m, dict) or not m.get("name"):
        return None
    ext = manifest_extensions(m)
    skills_root = plugin_dir / "skills"
    skills_count = (
        sum(1 for c in skills_root.iterdir() if c.is_dir() and (c / "SKILL.md").is_file())
        if skills_root.is_dir()
        else 0
    )
    admin_config = _ext_or_top(m, ext, "admin_config")
    return {
        "slug": _sanitize_id(str(m.get("name")), 100),
        "name": str(m.get("display_name") or m.get("name")),
        "version": str(m.get("version") or "1.0.0"),
        "description": str(m.get("description") or ""),
        "category": str(m.get("category") or ""),
        "icon": m.get("icon"),
        "skills_count": skills_count,
        "required_secrets": list(_ext_or_top(m, ext, "required_secrets") or []),
        "has_admin_config": isinstance(admin_config, dict)
        and bool((admin_config or {}).get("fields")),
    }


def _component_keys(components: Dict[str, Any], kind: str) -> List[str]:
    """Project declarative component entries to IDs for legacy catalog operations.

    Keep InstalledPlugin.component_ids unchanged: version/required/platform
    fields belong to the exported definition and runtime dependency closure.
    """
    values = components.get(kind) or []
    if not isinstance(values, list):
        values = [values]
    keys = []
    for value in values:
        if isinstance(value, dict):
            value = (
                value.get("id")
                or value.get("key")
                or value.get("skill_id")
                or value.get("server_id")
                or value.get("agent_id")
            )
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    return keys
