"""Resolve one authorized package; device selection never falls back by name."""

from dataclasses import dataclass
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import or_


@dataclass(frozen=True)
class Installation:
    install_id: str
    revision: str
    slug: str
    ui: dict
    package: Path


def resolve(db, slug, user_id, install_id=None):
    from core.harness.registry import device_catalog
    selected = device_catalog.resolve_plugin(slug, user_id, install_id=install_id)
    if selected is not None:
        return selected
    if device_catalog.active():
        raise HTTPException(404, "plugin_source_unavailable")
    from core.db.models import InstalledPlugin

    rows = (
        db.query(InstalledPlugin)
        .filter(
            InstalledPlugin.slug == slug,
            or_(InstalledPlugin.owner_user_id == user_id, InstalledPlugin.owner_user_id.is_(None)),
        )
        .all()
    )
    if install_id:
        rows = [row for row in rows if row.install_id == install_id]
    rows.sort(key=lambda row: row.owner_user_id is None)
    if not rows:
        raise HTTPException(404, "plugin_source_unavailable")
    row = rows[0]
    from core.capabilities.catalog.resolver import resolve_all_runtime_enabled
    from core.capabilities.plugins.packaging.sources import component_keys as _component_keys

    skills, _, mcps = resolve_all_runtime_enabled(db, user_id)
    cids = row.component_ids or {}
    from core.harness.registry.package_views import enabled as workspace_enabled

    if not (
        workspace_enabled(db, row, user_id)
        or set(_component_keys(cids, "skills")) & set(skills or [])
        or set(_component_keys(cids, "mcp")) & set(mcps or [])
    ):
        raise HTTPException(403, "plugin_not_enabled")
    ui = row.ui_contributions
    if not ui:
        raise HTTPException(404, "plugin_ui_unavailable")
    revision = (row.import_report or {}).get("package_revision")
    if revision:
        from core.capabilities.plugins.packaging.runtime_assets import materialize

        package = materialize(db, revision)
    else:
        from core.capabilities.plugins.packaging.sources import resolve_plugin_dir

        package = resolve_plugin_dir(slug)
    if package is None:
        raise HTTPException(409, "plugin_package_missing")
    return Installation(row.install_id, revision or row.version or "", slug, ui, Path(package))


def safe_path(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.exists():
        raise HTTPException(404, "package_asset_missing")
    return target
