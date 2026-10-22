"""Admin discovery resolves current installations without granting execution."""
from features.workbench import origin
from features.workbench.external import fail
from features.workbench.plugin_catalog import application_key
from features.workbench.protocol import validate_contributions
from core.db.models import InstalledPlugin, UserShadow
from core.capabilities.plugins.resources.installation import Installation
from core.capabilities.plugins.packaging.runtime_assets import materialize


def selections(db, actor):
    source = origin.current(db)
    from features.desktop.capabilities import device_catalog
    represented = set()
    if source["kind"] == "local":
        from features.desktop.capabilities import plugins, store
        from features.desktop.capabilities.dependency import component_hash
        from features.desktop.capabilities.local_plugin_runtime import enabled_for
        from features.workbench.installation_binding import DeviceRecord
        owners = {actor} | {r.user_id for r in db.query(UserShadow).all()}
        for owner in owners:
            for inst in device_catalog.installations("plugin", user_id=owner):
                if not inst.ready or inst.created_at is None:
                    continue
                component = store.get("plugin", inst.profile_id, inst.key, inst.resolved_revision)
                expected_hash = inst.payload.get("resolved_content_hash") or inst.content_hash
                if component is None or component_hash(component) != expected_hash:
                    continue
                manifest = plugins.load_manifest(component)
                package = component.path / "package"
                if not package.is_dir():
                    continue
                represented_id = inst.payload.get("db_install_id")
                represented_row = db.get(InstalledPlugin, represented_id) if represented_id else None
                stamp = represented_row.created_at if represented_row else inst.created_at
                record = DeviceRecord(inst.install_id, inst.key, stamp,
                                      manifest.get("ui_contributions") or {}, represented_id)
                selected = Installation(inst.install_id, inst.resolved_revision, inst.key,
                                        record.ui_contributions, package)
                represented.add(represented_id)
                for app in validate_contributions(selected.ui.get("contributes"),
                                                  {"source": "plugin"})["applications"]:
                    yield {
                        "applicationKey": application_key(record, app["id"], source),
                        "ownerUserId": owner, "origin": source, "enabled": enabled_for(inst, owner),
                        "revision": selected.revision, "title": app["title"],
                        "binding": {"installId": selected.install_id,
                                    "packageRevision": selected.revision, "origin": source},
                    }, selected
    if device_catalog.active():
        return
    for row in db.query(InstalledPlugin).order_by(InstalledPlugin.install_id).all():
        if row.install_id in represented:
            continue
        revision = (row.import_report or {}).get("package_revision")
        if not revision:
            continue  # No immutable package identity: not an installation configuration target.
        try:
            package = materialize(db, revision)
            selected = Installation(row.install_id, revision, row.slug,
                                    row.ui_contributions or {}, package)
            checked = validate_contributions(selected.ui.get("contributes"), {"source": "plugin"})
        except (OSError, ValueError):
            continue
        for app in checked["applications"]:
            yield {
                "applicationKey": application_key(row, app["id"], source),
                "ownerUserId": row.owner_user_id, "origin": source,
                 "enabled": _enabled(db, row, actor), "revision": revision, "title": app["title"],
                "binding": {"installId": selected.install_id,
                            "packageRevision": selected.revision, "origin": source},
            }, selected


def resolve_admin(db, actor, key, revision):
    for item, selected in selections(db, actor):
        if item["applicationKey"] == key:
            if revision != selected.revision:
                fail(409, "revision_conflict")
            return item, selected
    fail(404, "app_not_found")



def _enabled(db, row, actor):
    from features.workbench.installation_binding import exact
    from fastapi import HTTPException
    try:
        exact(db, row.slug, row.owner_user_id or actor, row.install_id)
        return True
    except HTTPException:
        return False
