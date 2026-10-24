"""Pure conversion plan. Stable legacy IDs preserve allowed_apps and auth bindings."""

import re
from collections import Counter
from copy import deepcopy

from features.workbench import external, preferences
from features.workbench.catalog import BUILTINS
from features.workbench.migration_snapshot import digest
from features.workbench.protocol import validate_application
from core.files.content.content_blocks_constants import DEFAULT_PAGE_CONFIG
from fastapi import HTTPException

FIXED = {"chat", "settings", "docs", "plan_mode"}


def configured_auth(auth, target):
    """Only explicit, usable canonical configurations satisfy the cutover gate.

    This verifies the stored configuration, not a third party's real login.
    Real target/IdP compatibility remains a separate deployment acceptance.
    """
    if (
        not isinstance(auth, dict)
        or type(auth.get("revision")) is not int
        or auth["revision"] <= 0
    ):
        return False
    mode = auth.get("mode")
    if mode == "independent":
        return set(auth) <= {"revision", "mode"}
    from features.workbench.external_auth import destination

    try:
        if mode == "shared_identity":
            provider = auth.get("identityProvider")
            login = auth.get("loginUrl")
            if not isinstance(provider, str) or not 1 <= len(provider) <= 200:
                return False
            if not isinstance(login, str) or not 1 <= len(login) <= 2048:
                return False
            destination(login, target)
            return set(auth) <= {"revision", "mode", "identityProvider", "loginUrl"}
        if mode == "exchange":
            required = {"audience", "callback", "initiationUrl", "secretHash", "ttlSeconds"}
            if not required <= set(auth) or set(auth) - required - {"revision", "mode"}:
                return False
            if not isinstance(auth["audience"], str) or not 1 <= len(auth["audience"]) <= 200:
                return False
            if not isinstance(auth["secretHash"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", auth["secretHash"]
            ):
                return False
            if type(auth["ttlSeconds"]) is not int or not 1 <= auth["ttlSeconds"] <= 60:
                return False
            for name in ("callback", "initiationUrl"):
                if not isinstance(auth[name], str) or not 1 <= len(auth[name]) <= 2048:
                    return False
                destination(auth[name], target)
            return True
    except (HTTPException, TypeError, ValueError):
        return False
    return False


def plan(source):
    blocks = source["blocks"]
    raw = blocks.get("app_config", {})
    apps = raw.get("apps", []) if isinstance(raw, dict) else []
    quarantine = []
    if "app_config" in blocks and (not isinstance(raw, dict) or set(raw) != {"apps"}):
        quarantine.append({"path": "app_config", "code": "invalid_config_shape"})
    if not isinstance(apps, list):
        quarantine.append({"path": "app_config.apps", "code": "invalid_list"})
        apps = []
    counts = Counter(
        a.get("id") for a in apps if isinstance(a, dict) and isinstance(a.get("id"), str)
    )
    converted, mapping, auth_gaps = {}, {}, []
    for index, app in enumerate(apps):
        path = f"app_config.apps[{index}]"
        identifier = app.get("id") if isinstance(app, dict) else None
        code = None
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
        ):
            code = "invalid_id"
        elif counts[identifier] != 1:
            code = "duplicate_id"
        elif identifier in BUILTINS or external.PREFIX + identifier in blocks:
            code = "namespace_collision"
        if not code and set(app) - {"id", "name", "description", "url", "icon", "enabled"}:
            code = "unknown_app_field"
        if code:
            quarantine.append({"path": path, "code": code})
            continue
        declaration = {
            "schemaVersion": "1.0.0",
            "id": identifier,
            "title": app.get("name") or identifier,
            "target": {"kind": "external", "mode": "browser", "url": app.get("url", "")},
        }
        if app.get("description"):
            declaration["description"] = app["description"]
        try:
            external.validate_url(declaration["target"]["url"])
        except (TypeError, ValueError, HTTPException):
            # Only URL validation errors are isolated; no raw exception text is reported.
            quarantine.append({"path": path, "code": "invalid_url"})
            continue
        if validate_application(declaration) or not isinstance(app.get("enabled", True), bool):
            quarantine.append({"path": path, "code": "invalid_declaration"})
            continue
        key = "legacy:" + identifier
        mapping[identifier] = key
        mapping[key] = key
        converted[external.PREFIX + identifier] = {
            "applicationKey": key,
            "declaration": declaration,
            "enabled": app.get("enabled", True),
            "revision": 1,
            "legacy": True,
            "legacyOrder": index,
            "legacyIcon": str(app.get("icon") or ""),
        }
        auth = blocks.get("workbench_external_auth:" + key)
        if not configured_auth(auth, declaration["target"]["url"]):
            auth_gaps.append(
                {"application": digest(key)[:16], "code": "auth_configuration_required"}
            )
    mapping.update({key: "builtin:" + key for key in BUILTINS})
    mapping.update({"builtin:" + key: "builtin:" + key for key in BUILTINS})
    for key, item in blocks.items():
        if key.startswith(external.PREFIX) and isinstance(item, dict):
            reference = item.get("applicationKey")
            if isinstance(reference, str):
                mapping[reference] = reference

    def refs(values, path):
        if not isinstance(values, list):
            quarantine.append({"path": path, "code": "invalid_list"})
            return []
        result = []
        for index, value in enumerate(values):
            if isinstance(value, str) and value in mapping and mapping[value] not in result:
                result.append(mapping[value])
            else:
                quarantine.append(
                    {"path": f"{path}[{index}]", "code": "unknown_or_duplicate_reference"}
                )
        return result

    page = deepcopy(blocks.get("page_config", DEFAULT_PAGE_CONFIG))
    if not isinstance(page, dict):
        raise ValueError("invalid_page_config")
    nav = page.get("navigation", {})
    if not isinstance(nav, dict):
        raise ValueError("invalid_navigation_config")
    layout = deepcopy(blocks.get("workbench_policy"))
    if layout is None:
        sidebar = refs(
            nav.get("sidebar_items", DEFAULT_PAGE_CONFIG["navigation"]["sidebar_items"]),
            "page_config.navigation.sidebar_items",
        )
        menu = refs(
            nav.get("menu_items", DEFAULT_PAGE_CONFIG["navigation"]["menu_items"]),
            "page_config.navigation.menu_items",
        )
        # Utility placement is fixed in WB-04; ordering regular entries is preserved.
        layout = {
            "revision": 1,
            "defaultPinned": [k for k in sidebar if k[8:] not in FIXED],
            "forced": [],
            "hidden": [],
            "legacyMenu": menu,
        }
    changed_users, navigation = {}, {}
    for user_id, metadata in source["users"].items():
        if not isinstance(metadata, dict):
            continue
        prefs = metadata.get("preferences")
        if not isinstance(prefs, dict) or not ({"pinned_apps", "app_order"} & set(prefs)):
            continue
        location = "preferences:" + digest(user_id)[:16]
        pinned = (
            None
            if prefs.get("pinned_apps") is None
            else refs(prefs["pinned_apps"], location + ".pinned_apps")
        )
        order = refs(prefs.get("app_order", []), location + ".app_order")
        key = preferences.identity(user_id, "web")
        # Already-saved modern preferences always win.
        if key not in blocks:
            navigation[key] = {
                "revision": 1,
                "pinned": pinned,
                "order": order,
                "environment": "web",
            }
        value = deepcopy(metadata)
        value["preferences"].pop("pinned_apps", None)
        value["preferences"].pop("app_order", None)
        changed_users[user_id] = value
    if isinstance(nav, dict):
        page["navigation"] = {
            k: v for k, v in nav.items() if k not in {"sidebar_items", "menu_items"}
        }
    plugin_versions = Counter(
        str(p["ui"].get("version", "none")) if isinstance(p.get("ui"), dict) else "none"
        for p in source["plugins"].values()
    )
    permission_references = []
    layers = [("user:" + digest(uid)[:16], meta) for uid, meta in source["users"].items()]
    layers += [
        ("role:" + digest(uid)[:16], meta)
        for uid, meta in source.get("permissions", {}).get("roles", {}).items()
    ]
    layers += [
        ("team:" + digest(uid)[:16], meta)
        for uid, meta in source.get("permissions", {}).get("teams", {}).items()
    ]
    known_permissions = set(mapping) | {"prompt_hub"}
    for location, meta in layers:
        allowed = meta.get("allowed_apps") if isinstance(meta, dict) else None
        if not isinstance(allowed, list):
            continue
        seen = set()
        for index, value in enumerate(allowed):
            if not isinstance(value, str) or value not in known_permissions or value in seen:
                permission_references.append(
                    {
                        "path": f"{location}.allowed_apps[{index}]",
                        "code": "unmapped_permission_preserved",
                    }
                )
            if isinstance(value, str):
                seen.add(value)
    report = {
        "schemaVersion": 1,
        "externalCount": len(converted),
        "quarantined": len(quarantine),
        "quarantine": quarantine,
        "authGaps": auth_gaps,
        "pluginVersions": dict(plugin_versions),
        "users": len(source["users"]),
        "permissions": "identity_mapping_unchanged",
        "permissionReferences": permission_references,
    }
    return {
        "external": converted,
        "policy": layout,
        "page": page,
        "users": changed_users,
        "navigation": navigation,
        "report": report,
    }
