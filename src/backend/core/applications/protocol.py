"""Validate the shared workbench Schema and cross-field references. No Node service."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft7Validator

_ROOT = Path(__file__).parent / "schemas"
_APPLICATION = Draft7Validator(json.loads((_ROOT / "application.schema.json").read_text()))
_MODULE = Draft7Validator(json.loads((_ROOT / "module.schema.json").read_text()))
_POLICY = json.loads((_ROOT / "host.json").read_text())
HOST_CAPABILITIES = _POLICY["capabilities"]
UI_VERSION = _POLICY["uiVersion"]


def _issue(path, code, message):
    return {"path": path, "code": code, "message": message}


def _structure(validator, value):
    # Never include jsonschema messages: they contain the rejected input.
    return [
        _issue(
            "/" + "/".join(str(p) for p in e.absolute_path), "structure", "Invalid field structure"
        )
        for e in validator.iter_errors(value)
    ]


def validate_application(value, context=None):
    context = context or {}
    errors = _structure(_APPLICATION, value)
    if errors:
        return errors
    target = value["target"]

    def reject(path, code, message):
        errors.append(_issue(path, code, message))

    if context.get("source") == "plugin" and target["kind"] != "plugin":
        reject(
            "/target/kind",
            "untrusted_target",
            "Plugin applications must reference their own modules",
        )
    if target["kind"] == "builtin" and target["componentId"] not in context.get("componentIds", []):
        reject("/target/componentId", "unknown_component", "Unknown component")
    if target["kind"] == "command" and target["commandId"] not in context.get("commandIds", []):
        reject("/target/commandId", "unknown_command", "Unknown command")

    def module_exists(mid):
        matches = [m for m in context.get("modules", []) if m["id"] == mid]
        return len(matches) == 1 and matches[0]["surface"] == "workspace"

    if target["kind"] == "plugin" and not module_exists(target["moduleId"]):
        reject("/target/moduleId", "invalid_module", "Expected one workspace module")
    ids, paths = set(), set()
    for i, view in enumerate(value.get("views", [])):
        if view["id"] in ids:
            reject(f"/views/{i}/id", "duplicate_id", "Duplicate view ID")
        if view["path"] in paths:
            reject(f"/views/{i}/path", "duplicate_path", "Duplicate view path")
        ids.add(view["id"])
        paths.add(view["path"])
        if view.get("moduleId") and (
            target["kind"] != "plugin" or not module_exists(view["moduleId"])
        ):
            reject(f"/views/{i}/moduleId", "invalid_module", "Expected one workspace module")
    if target["kind"] == "external":
        try:
            url = urlsplit(target["url"])
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or "\\" in target["url"]
            ):
                raise ValueError()
            _ = url.port
            if url.hostname.replace(".", "").isdigit():
                ipaddress.IPv4Address(url.hostname)
            if ":" in url.hostname:
                ipaddress.IPv6Address(url.hostname)
        except ValueError:
            reject("/target/url", "unsafe_url", "Expected an HTTP URL without credentials")
    compatibility = value.get("compatibility", {})
    version = compatibility.get("protocolVersion")
    if version and (version.split(".")[0] != "1" or int(version.split(".")[1]) > 0):
        reject(
            "/compatibility/protocolVersion", "incompatible_version", "Unsupported protocol version"
        )
    for i, capability in enumerate(compatibility.get("requiredCapabilities", [])):
        if capability not in context.get("capabilities", HOST_CAPABILITIES):
            reject(
                f"/compatibility/requiredCapabilities/{i}",
                "unsupported_capability",
                "Required capability is unavailable",
            )
    return errors


def validate_contributions(value, context=None):
    context = context or {}
    result = {"applications": [], "modules": [], "errors": []}
    errors = result["errors"]
    if not isinstance(value, dict):
        errors.append(_issue("/", "structure", "Expected contributions object"))
        return result
    for kind in ("modules", "applications"):
        if kind in value and (not isinstance(value[kind], list) or len(value[kind]) > 200):
            errors.append(_issue("/" + kind, "structure", "Expected at most 200 entries"))

    def entries(kind):
        items = value.get(kind, [])
        return items if isinstance(items, list) and len(items) <= 200 else []

    def duplicate(items, item):
        return (
            isinstance(item, dict)
            and "id" in item
            and sum(isinstance(e, dict) and e.get("id") == item["id"] for e in items) > 1
        )

    for i, module in enumerate(entries("modules")):
        prefix = f"/modules/{i}"
        problems = _structure(_MODULE, module)
        if problems:
            errors.extend({**e, "path": prefix + e["path"].rstrip("/")} for e in problems)
            continue
        if duplicate(entries("modules"), module):
            errors.append(_issue(prefix + "/id", "duplicate_id", "Duplicate module ID"))
            continue
        valid = True
        for j, grant in enumerate(module.get("grants", [])):
            if grant not in context.get("capabilities", HOST_CAPABILITIES) and not grant.startswith(
                "data_source:"
            ):
                errors.append(
                    _issue(
                        prefix + f"/grants/{j}",
                        "unsupported_capability",
                        "Required capability is unavailable",
                    )
                )
                valid = False
            if (
                grant.startswith("data_source:")
                and sum(
                    isinstance(s, dict) and s.get("id") == grant[12:]
                    for s in entries("data_sources")
                )
                != 1
            ):
                errors.append(
                    _issue(prefix + f"/grants/{j}", "invalid_reference", "Unknown data source")
                )
                valid = False
        if valid:
            result["modules"].append(
                {
                    **module,
                    "title": module.get("title", module["id"]),
                    "grants": module.get("grants", []),
                }
            )
    for i, app in enumerate(entries("applications")):
        prefix = f"/applications/{i}"
        if duplicate(entries("applications"), app):
            errors.append(_issue(prefix + "/id", "duplicate_id", "Duplicate application ID"))
            continue
        problems = validate_application(app, {**context, "modules": result["modules"]})
        errors.extend({**e, "path": prefix + e["path"]} for e in problems)
        if not problems:
            projected = {k: v for k, v in app.items() if k in _APPLICATION.schema["properties"]}
            if "compatibility" in app:
                allowed = _APPLICATION.schema["properties"]["compatibility"]["properties"]
                projected["compatibility"] = {
                    k: v for k, v in app["compatibility"].items() if k in allowed
                }
            result["applications"].append(projected)
    return result
