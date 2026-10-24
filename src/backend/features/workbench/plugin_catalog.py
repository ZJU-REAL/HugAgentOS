"""User-scoped installed application projection and exact package launch."""

from copy import deepcopy
from hashlib import sha256
from urllib.parse import quote

from features.workbench.protocol import validate_contributions
from features.workbench import origin
from core.db.models import InstalledPlugin
from core.infra.time import as_utc
from features.workbench.installation_binding import exact as resolve, device_records
from fastapi import HTTPException
from sqlalchemy import or_

WORKSPACE_CAPABILITIES = {
    "background",
    "resource.attach",
    "resource.close",
    "data.query",
    "file.pick",
    "host.info",
    "platform.runtimeCapabilities",
    "badge.update",
    "state.save",
    "resource.command",
    "platform.openExternal",
    "applications",
    "state.load",
    "resource.open",
    "navigation.open",
    "file.save",
    "chat.compose",
    "notification.show",
    "workspace.navigate",
    "resource.lease",
    "workspace",
    "lifecycle.setDirty",
}


def application_key(row, app_id, source):
    # created_at survives upgrades but changes on uninstall/reinstall.
    # Hash the full binding because raw IDs contain @ and can exceed key limits.
    stamp = as_utc(row.created_at).strftime("%Y%m%dT%H%M%S.%fZ")
    installation = sha256(row.install_id.encode()).hexdigest()
    return f"plugin:{source['kind']}:{source['id']}:{installation}:{stamp}:{app_id}"


def items(db, user_id, include_disabled=False):
    source = origin.current(db)
    records = (
        db.query(InstalledPlugin)
        .filter(
            or_(InstalledPlugin.owner_user_id == user_id, InstalledPlugin.owner_user_id.is_(None))
        )
        .all()
    )
    from features.desktop.capabilities import device_catalog

    if device_catalog.active():
        records = []
    devices = device_records(db, user_id) if source["kind"] == "local" else []
    represented = {record.represented_install_id for record in devices}
    records = [row for row in records if row.install_id not in represented] + devices
    result = []
    for record in records:
        if not record.ui_contributions or record.ui_contributions.get("version") != 3:
            continue
        try:
            selected = resolve(db, record.slug, user_id, record.install_id)
        except HTTPException as error:
            if include_disabled and error.detail == "plugin_not_enabled":
                checked = validate_contributions(
                    record.ui_contributions.get("contributes"), {"source": "plugin"}
                )
                for declaration in checked["applications"]:
                    result.append(
                        {
                            "applicationKey": application_key(record, declaration["id"], source),
                            "revision": getattr(record, "version", "")
                            or (getattr(record, "import_report", None) or {}).get(
                                "package_revision", ""
                            ),
                            "enabled": False,
                            "origin": source,
                            "declaration": deepcopy(declaration),
                        }
                    )
            continue
        checked = validate_contributions(selected.ui.get("contributes"), {"source": "plugin"})
        for declaration in checked["applications"]:
            available_capabilities = WORKSPACE_CAPABILITIES
            if not declaration.get("lifecycle", {}).get("stateSchemaVersion"):
                available_capabilities = available_capabilities - {"state.load", "state.save"}
            result.append(
                {
                    "applicationKey": application_key(record, declaration["id"], source),
                    "revision": selected.revision,
                    "enabled": True,
                    "origin": source,
                    "unavailableReason": origin.execution_reason(declaration, source)
                    or (
                        "host_incompatible"
                        if set(declaration.get("compatibility", {}).get("requiredCapabilities", []))
                        - available_capabilities
                        else None
                    ),
                    "binding": {
                        "installId": selected.install_id,
                        "packageRevision": selected.revision,
                        "slug": selected.slug,
                        "executionScope": source["kind"],
                        "origin": source,
                    },
                    "declaration": deepcopy(declaration),
                }
            )
    return result


def launch(db, user_id, item, module_id=None):
    binding = item["binding"]
    source = origin.current(db)
    if binding.get("origin") != source or binding["executionScope"] != source["kind"]:
        raise HTTPException(409, detail={"code": "origin_mismatch"})
    selected = resolve(db, binding["slug"], user_id, binding["installId"])
    if selected.revision != binding["packageRevision"]:
        raise HTTPException(409, detail={"code": "revision_conflict"})
    checked = validate_contributions(selected.ui.get("contributes"), {"source": "plugin"})
    app = next((a for a in checked["applications"] if a["id"] == item["declaration"]["id"]), None)
    if app is None:
        raise HTTPException(409, detail={"code": "host_incompatible"})
    allowed = {app["target"]["moduleId"]} | {
        view["moduleId"] for view in app.get("views", []) if view.get("moduleId")
    }
    module_id = module_id or app["target"]["moduleId"]
    if module_id not in allowed:
        raise HTTPException(404, detail={"code": "app_view_not_found"})
    module = next(m for m in checked["modules"] if m["id"] == module_id)
    from core.capabilities.plugins.resources.installation import safe_path

    target = safe_path(selected.package, module["entry"])
    if not target.is_file():
        raise HTTPException(404, detail={"code": "package_asset_missing"})
    return {
        "applicationKey": item["applicationKey"],
        "revision": selected.revision,
        "target": app["target"],
        "binding": binding,
        "module": module,
        "assetPath": "/v1/workbench/applications/"
        + quote(item["applicationKey"], safe="")
        + "/packages/"
        + quote(selected.revision, safe="")
        + "/web/"
        + module["entry"][4:],
        "context": {
            "applicationKey": item["applicationKey"],
            "installId": selected.install_id,
            "packageRevision": selected.revision,
            "origin": source,
            "executionScope": source["kind"],
        },
    }


def administrative_items(db):
    source = origin.current(db)
    result = []
    for row in db.query(InstalledPlugin).all():
        ui = row.ui_contributions or {}
        if ui.get("version") != 3:
            continue
        checked = validate_contributions(ui.get("contributes"), {"source": "plugin"})
        for declaration in checked["applications"]:
            available_capabilities = WORKSPACE_CAPABILITIES
            if not declaration.get("lifecycle", {}).get("stateSchemaVersion"):
                available_capabilities = available_capabilities - {"state.load", "state.save"}
            result.append(
                {
                    "applicationKey": application_key(row, declaration["id"], source),
                    "revision": 1,
                    "enabled": True,
                    "declaration": declaration,
                }
            )
    return result
