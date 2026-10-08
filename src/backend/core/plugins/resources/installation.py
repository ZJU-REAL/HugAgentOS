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
    from core.capabilities import device_catalog, store
    from core.capabilities.device_plugin_catalog import _projected_plugin
    from core.capabilities.local_plugin_runtime import enabled_for
    from core.capabilities.plugins import load_manifest
    candidate = _projected_plugin(install_id or slug, user_id=user_id)
    if candidate is not None:
        if candidate.key != slug or not candidate.ready or not enabled_for(candidate, user_id):
            raise HTTPException(403, "plugin_not_ready")
        component = store.get("plugin", candidate.profile_id, candidate.key, candidate.resolved_revision)
        if component is None:
            raise HTTPException(409, "plugin_package_missing")
        manifest = load_manifest(component)
        package = component.path / "package"
        if not package.is_dir():
            raise HTTPException(409, "plugin_package_missing")
        return Installation(candidate.install_id, candidate.resolved_revision, slug, manifest.get("ui_contributions") or {}, package)
    if device_catalog.active():
        raise HTTPException(404, "plugin_source_unavailable")
    from core.db.models import InstalledPlugin
    rows = db.query(InstalledPlugin).filter(InstalledPlugin.slug == slug, or_(InstalledPlugin.owner_user_id == user_id, InstalledPlugin.owner_user_id.is_(None))).all()
    if install_id:
        rows = [row for row in rows if row.install_id == install_id]
    rows.sort(key=lambda row: row.owner_user_id is None)
    if not rows:
        raise HTTPException(404, "plugin_source_unavailable")
    row = rows[0]
    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.plugins.packaging.sources import _component_keys
    skills, _, mcps = resolve_all_runtime_enabled(db, user_id)
    cids = row.component_ids or {}
    if not (set(_component_keys(cids, "skills")) & set(skills or []) or set(_component_keys(cids, "mcp")) & set(mcps or [])):
        raise HTTPException(403, "plugin_not_enabled")
    ui = row.ui_contributions
    if not ui:
        raise HTTPException(404, "plugin_ui_unavailable")
    revision = (row.import_report or {}).get("package_revision")
    if revision:
        from core.plugins.packaging.runtime_assets import materialize
        package = materialize(db, revision)
    else:
        from core.plugins.packaging.sources import _resolve_plugin_dir
        package = _resolve_plugin_dir(slug)
    if package is None:
        raise HTTPException(409, "plugin_package_missing")
    return Installation(row.install_id, revision or row.version or "", slug, ui, Path(package))

def safe_path(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.exists():
        raise HTTPException(404, "package_asset_missing")
    return target
