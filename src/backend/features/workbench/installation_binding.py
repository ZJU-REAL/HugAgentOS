"""Exact workbench installation adapter over the existing two installation stores."""

from dataclasses import dataclass
from datetime import datetime

from core.capabilities.plugins.resources.installation import Installation, resolve
from fastapi import HTTPException


@dataclass(frozen=True)
class DeviceRecord:
    install_id: str
    slug: str
    created_at: datetime
    ui_contributions: dict
    represented_install_id: str | None = None


def device_records(db, user_id):
    from features.desktop.capabilities import device_catalog, plugins, store
    from features.desktop.capabilities.local_plugin_runtime import enabled_for
    from core.harness.registry.run_capabilities import has_run_capabilities

    if not has_run_capabilities():
        return []
    result = []
    for inst in device_catalog.installations("plugin", user_id=user_id):
        if not inst.ready or not enabled_for(inst, user_id) or inst.created_at is None:
            continue
        component = store.get("plugin", inst.profile_id, inst.key, inst.resolved_revision)
        if component is None:
            continue
        try:
            manifest = plugins.load_manifest(component)
        except (OSError, ValueError):
            continue
        from core.db.models import InstalledPlugin

        represented = inst.payload.get("db_install_id")
        row = db.get(InstalledPlugin, represented) if represented else None
        stamp = row.created_at if row is not None else inst.created_at
        result.append(
            DeviceRecord(
                inst.install_id,
                inst.key,
                stamp,
                manifest.get("ui_contributions") or {},
                represented,
            )
        )
    return result


def exact(db, slug, user_id, install_id):
    from features.desktop.capabilities import device_catalog, plugins, store
    from features.desktop.capabilities.dependency import component_hash
    from features.desktop.capabilities.local_plugin_runtime import enabled_for
    from core.harness.registry.run_capabilities import has_run_capabilities

    # Legacy resolver also accepts aliases. Workbench has no alias authority.
    if has_run_capabilities():
        candidates = [
            inst
            for inst in device_catalog.installations("plugin", user_id=user_id)
            if inst.install_id == install_id
        ]
        if candidates:
            inst = candidates[0]
            if inst.key != slug or not inst.ready or not enabled_for(inst, user_id):
                raise HTTPException(403, detail={"code": "installation_not_ready"})
            component = store.get("plugin", inst.profile_id, inst.key, inst.resolved_revision)
            expected_hash = inst.payload.get("resolved_content_hash") or inst.content_hash
            if component is None or component_hash(component) != expected_hash:
                raise HTTPException(409, detail={"code": "package_revision_invalid"})
            package = component.path / "package"
            if not package.is_dir():
                raise HTTPException(409, detail={"code": "package_asset_missing"})
            manifest = plugins.load_manifest(component)
            return Installation(
                inst.install_id,
                inst.resolved_revision,
                inst.key,
                manifest.get("ui_contributions") or {},
                package,
            )
        if device_catalog.active():
            raise HTTPException(404, detail={"code": "app_not_found"})
    selected = resolve(db, slug, user_id, install_id)
    if selected.install_id != install_id or selected.slug != slug:
        raise HTTPException(404, detail={"code": "app_not_found"})
    return selected
