"""Plugin ui responsibilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.models import InstalledPlugin
from core.plugins.management import queries as plugin_queries
from core.plugins.packaging import sources as plugin_sources
from core.plugins.ui.contract import public_contributions
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


def refresh_builtin_ui_contributions(db: Session) -> int:
    """Sync every builtin install's ``ui_contributions`` with its bundle manifest.

    The column is written at install time, so it is NULL on rows installed
    before the UI-contribution contract existed, and goes stale whenever a
    bundle's ``ui`` declaration is edited without a version bump. Unlike skills
    and MCP components, the UI declaration carries no user state (no enable
    flags, no secrets), so refreshing it in place at startup is safe — and it
    also covers per-user private installs, which the version-gated EE plugin
    bootstrap never touches.

    Returns the number of rows updated.
    """
    import json

    from core.plugins.packaging.importer import _ext_or_top, manifest_extensions
    from core.plugins.ui.contract import normalize_ui

    # One manifest read per distinct slug, not per install row.
    ui_by_slug: Dict[str, Optional[Dict[str, Any]]] = {}

    def _bundle_ui(slug: str) -> Optional[Dict[str, Any]]:
        if slug not in ui_by_slug:
            ui: Optional[Dict[str, Any]] = None
            plugin_dir = plugin_sources._resolve_plugin_dir(slug)
            if plugin_dir is not None:
                try:
                    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
                    ui, _dropped = normalize_ui(
                        _ext_or_top(manifest, manifest_extensions(manifest), "ui")
                    )
                except (OSError, ValueError):
                    ui = None
            ui_by_slug[slug] = ui
        return ui_by_slug[slug]

    updated = 0
    rows = db.query(InstalledPlugin).filter(InstalledPlugin.source == "builtin").all()
    for row in rows:
        plugin_dir = plugin_sources._resolve_plugin_dir(row.slug)
        if plugin_dir is None:
            # Bundle removed from this build (e.g. CE tree): leave the row alone.
            continue
        desired = _bundle_ui(row.slug)
        if row.ui_contributions != desired:
            row.ui_contributions = desired
            flag_modified(row, "ui_contributions")
            updated += 1
    if updated:
        db.commit()
        logger.info("plugin_ui: refreshed ui_contributions for %d installed plugin(s)", updated)
    return updated


def resolve_ui_contributions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    """UI contributions of every plugin this user has installed **and enabled**.

    The frontend renders purely from this list, so disabling or uninstalling a
    plugin withdraws its interface on the next refresh without any host-side
    branch knowing that plugin exists.

    Credential-bearing fields (a data source's upstream url / auth header) are
    stripped by ``public_contributions`` — the browser only learns that a source
    id exists and what parameters it takes.
    """
    enabled_ids = [
        row["install_id"]
        for row in plugin_queries.list_installed(
            db, owner_user_id=owner_user_id, include_global=True
        )
        if row.get("enabled")
    ]
    if not enabled_ids:
        return []
    # One batched query instead of re-fetching each row list_installed already
    # loaded (the dict projection drops ui_contributions, so it must be re-read).
    records = db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(enabled_ids)).all()
    by_id = {r.install_id: r for r in records}
    out: List[Dict[str, Any]] = []
    for install_id in enabled_ids:
        record = by_id.get(install_id)
        if record is None or not record.ui_contributions:
            continue
        public = public_contributions(record.ui_contributions, slug=record.slug)
        if public.get("contributes"):
            out.append(public)
    return out


def resolve_installed_ui(db: Session, slug: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    """Raw (server-side) UI contributions for one slug the user may actually use.

    Used by the data proxy and the module host: both must confirm the caller
    really has this plugin installed and enabled before acting on its
    declaration, otherwise knowing a slug would be enough to drive the proxy.

    Hot path: the module host runs this once per static asset, so it queries the
    one slug directly instead of enumerating every installed plugin (which also
    re-reads each bundle's plugin.json for display metadata).
    """
    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from sqlalchemy import or_

    rows = (
        db.query(InstalledPlugin)
        .filter(
            InstalledPlugin.slug == slug,
            or_(
                InstalledPlugin.owner_user_id == owner_user_id,
                InstalledPlugin.owner_user_id.is_(None),
            ),
        )
        .all()
    )
    if not rows:
        return None
    # Same precedence as list_installed's dedup: an admin global install wins
    # over the user's private one.
    rows.sort(key=lambda r: r.owner_user_id is not None)
    row = rows[0]
    if not row.ui_contributions:
        return None
    # Enabled = any component effectively enabled for this user (per-user
    # overrides included); resolve_all_runtime_enabled is TTL-cached.
    eff_skills, _eff_agents, eff_mcps = resolve_all_runtime_enabled(db, owner_user_id)
    cids = row.component_ids or {}
    enabled_skills = set(eff_skills or [])
    enabled_mcps = set(eff_mcps or [])
    enabled = any(
        s in enabled_skills for s in (plugin_sources._component_keys(cids, "skills"))
    ) or any(m in enabled_mcps for m in (plugin_sources._component_keys(cids, "mcp")))
    return row.ui_contributions if enabled else None
