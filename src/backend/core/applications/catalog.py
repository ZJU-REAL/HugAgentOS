"""Trusted current applications and effective discovery/launch decisions.

allowed_apps retains its existing domain: mode commands, automation and external
apps. It does not become a new whitelist for basic chat, files, projects or settings.
Plugin declarations are not executable here until their installation host exists.
"""

from copy import deepcopy

from core.applications import external, policy, origin
from core.auth.capabilities import resolve_user_capabilities
from core.config.local_mode import local_mode_enabled
from core.db.models import ContentBlock

BUILTINS = {
    "chat": ("对话", "builtin", "chat"),
    "projects": ("项目", "builtin", "projects"),
    "my_space": ("我的空间", "builtin", "my_space"),
    "ability_center": ("能力中心", "builtin", "ability_center"),
    "app_center": ("应用中心", "builtin", "app_center"),
    "automation": ("定时任务", "builtin", "automation"),
    "sites": ("站点", "builtin", "sites"),
    "lab": ("实验室", "builtin", "lab"),
    "settings": ("设置", "builtin", "settings"),
    "docs": ("帮助", "builtin", "docs"),
    "plan_mode": ("计划模式", "command", "enter_plan"),
}
WHITELIST_BUILTINS = {"automation", "plan_mode"}


def definitions(db, user_id=None, include_disabled=False):
    values = []
    for identifier, (title, kind, target) in BUILTINS.items():
        values.append(
            {
                "applicationKey": "builtin:" + identifier,
                "revision": 1,
                "enabled": True,
                "declaration": {
                    "schemaVersion": "1.0.0",
                    "id": identifier,
                    "title": title,
                    "target": {
                        "kind": kind,
                        "componentId" if kind == "builtin" else "commandId": target,
                    },
                },
            }
        )
    external_values = external.items(db)
    for item in external_values:
        if item.get("legacyIcon") and not item["declaration"].get("icon"):
            item["declaration"] = {
                **item["declaration"], "icon": {"light": item["legacyIcon"]}
            }
    values.extend(external_values)
    from core.applications.migration_compat import active, app_config

    config = {"apps": []} if active(db) else app_config(db)
    for app in config["apps"]:
        values.append(
            {
                "applicationKey": "legacy:" + app["id"],
                "revision": 1,
                "enabled": app["enabled"],
                "legacy": True,
                "authenticationRequired": db.get(
                    ContentBlock, "workbench_external_auth:" + "legacy:" + app["id"]
                )
                is None,
                "declaration": {
                    "schemaVersion": "1.0.0",
                    "id": app["id"],
                    "title": app["name"],
                    "description": app["description"],
                    **({"icon": {"light": app["icon"]}} if app.get("icon") else {}),
                    "target": {"kind": "external", "mode": "browser", "url": app["url"]},
                },
            }
        )
    if user_id is not None:
        from core.applications import plugin_catalog

        values.extend(plugin_catalog.items(db, user_id, include_disabled=include_disabled))
    return values


def permitted(item, caps, layout):
    key = item["applicationKey"]
    identifier = key if key.startswith("plugin:") else item["declaration"]["id"]
    if key in layout["hidden"]:
        return False
    if key.startswith("builtin:") and identifier not in WHITELIST_BUILTINS:
        return identifier not in {"lab", "sites"} or caps["lab_enabled"]
    allowed = caps["allowed_apps"]
    return allowed is None or identifier in allowed


def project(item, layout):
    value = deepcopy(item)
    reason = "app_disabled" if not item["enabled"] else item.get("unavailableReason")
    if reason is None and item.get("authenticationRequired"):
        reason = "auth_configuration_required"
    if item["applicationKey"] == "builtin:my_space" and local_mode_enabled():
        reason = "origin_unavailable"
    value["availability"] = {"available": reason is None, "reason": reason}
    value["navigation"] = {
        "defaultPinned": item["applicationKey"] in layout["defaultPinned"],
        "forced": item["applicationKey"] in layout["forced"],
    }
    return value


def directory(db, user_id):
    caps = resolve_user_capabilities(db, user_id)
    layout = policy.read(db)
    values = [
        project(item, layout) for item in definitions(db, user_id) if permitted(item, caps, layout)
    ]
    visible = {i["applicationKey"] for i in values}
    return {
        "origin": origin.current(db),
        "items": values,
        "policy": {
            "revision": layout["revision"],
            "defaultPinned": [k for k in layout["defaultPinned"] if k in visible],
            "forced": [k for k in layout["forced"] if k in visible],
        },
    }


def readable(db, user_id, key, include_disabled=False):
    caps = resolve_user_capabilities(db, user_id)
    layout = policy.read(db)
    for item in definitions(db, user_id, include_disabled=include_disabled):
        if item["applicationKey"] == key and permitted(item, caps, layout):
            return project(item, layout)
    external.fail(404, "app_not_found")
