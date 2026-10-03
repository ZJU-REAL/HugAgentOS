"""Plugin admin config responsibilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.plugins.packaging import sources as plugin_sources
from core.plugins.packaging.importer import _ext_or_top, manifest_extensions, normalize_plugin_dir

logger = logging.getLogger(__name__)


def _admin_config_for_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Locate the plugin bundle by slug and read its admin_config declaration; None if absent."""
    plugin_dir = plugin_sources._resolve_plugin_dir(slug)
    if plugin_dir is None:
        return None
    try:
        return normalize_plugin_dir(plugin_dir).admin_config
    except Exception:  # noqa: BLE001
        return None


def _connection_for_slug(slug: str) -> Optional[str]:
    """Read a plugin's account connection type by slug (e.g. dingtalk / lark); None if absent.

    Like admin_config, read live from the bundle at detail-view time, not
    stored in a DB column — imported plugins (whose bundle no longer exists)
    naturally return None without affecting display.
    """
    plugin_dir = plugin_sources._resolve_plugin_dir(slug)
    if plugin_dir is None:
        return None
    try:
        import json

        m = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        conn = _ext_or_top(m, manifest_extensions(m), "connection")
        return str(conn).strip() if conn else None
    except Exception:  # noqa: BLE001
        return None


def _has_admin_config_for_slug(slug: str) -> bool:
    """Lightweight check for whether a plugin declares admin_config (reads only plugin.json, no full normalize)."""
    plugin_dir = plugin_sources._resolve_plugin_dir(slug)
    if plugin_dir is None:
        return False
    try:
        import json

        m = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        ac = _ext_or_top(m, manifest_extensions(m), "admin_config")
        return isinstance(ac, dict) and bool(ac.get("fields"))
    except Exception:  # noqa: BLE001
        return False


def _is_set(val: Optional[str]) -> bool:
    return bool((val or "").strip())


def _admin_config_configured(mode: str, sets: List[bool]) -> bool:
    """mode=any → ready if any field is set; mode=all → all fields must be set."""
    if not sets:
        return False
    return any(sets) if mode == "any" else all(sets)


def _admin_config_view(
    admin_config: Optional[Dict[str, Any]], *, with_values: bool
) -> Optional[Dict[str, Any]]:
    """Compute the current admin_config state.

    with_values=False (user side, read-only): returns only is_set +
    configured, **never real values**.
    with_values=True (admin editing): includes current values — a set secret
    returns the mask ``****``, an unset one returns empty; non-secrets return
    real values for the admin to view/edit.
    """
    if not admin_config:
        return None
    from core.services.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()
    fields: List[Dict[str, Any]] = []
    sets: List[bool] = []
    for f in admin_config.get("fields") or []:
        raw = svc.get(f["key"])
        s = _is_set(raw)
        sets.append(s)
        item = {
            "key": f["key"],
            "label": f["label"],
            "secret": f["secret"],
            "description": f["description"],
            "is_set": s,
        }
        if with_values:
            item["value"] = ("****" if s else "") if f["secret"] else (raw or "")
        fields.append(item)
    mode = admin_config.get("mode") or "all"
    return {
        "mode": mode,
        "group": admin_config.get("group") or "",
        "hint": admin_config.get("hint") or "",
        "configured": _admin_config_configured(mode, sets),
        "fields": fields,
    }


def get_plugin_admin_config(slug: str) -> Dict[str, Any]:
    """Admin side: get a plugin's admin config (schema + current values, secrets masked)."""
    ac = _admin_config_for_slug(slug)
    if not ac:
        raise ResourceNotFoundError("plugin_admin_config", slug)
    return _admin_config_view(ac, with_values=True)


def set_plugin_admin_config(
    slug: str, values: Dict[str, str], *, updated_by: str = "admin"
) -> Dict[str, Any]:
    """Admin side: write a plugin's admin config. Only field keys the plugin
    declared may be written (prevents privilege escalation into writing
    arbitrary SystemConfig); masked secret values (containing ``****``) are
    skipped by bulk_set and never overwrite the real value."""
    ac = _admin_config_for_slug(slug)
    if not ac:
        raise ResourceNotFoundError("plugin_admin_config", slug)
    allowed = {f["key"] for f in (ac.get("fields") or [])}
    items = [{"key": str(k), "value": v} for k, v in (values or {}).items() if k in allowed]
    if not items:
        raise BadRequestError(message="没有可写入的配置项（key 不属于该插件）")
    from core.services.system_config import SystemConfigService

    SystemConfigService.get_instance().bulk_set(items, updated_by=updated_by)
    return _admin_config_view(ac, with_values=True)
