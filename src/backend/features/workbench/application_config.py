"""Explicit administrator values for one origin/install/application/revision.

No global field registration or global configuration lookup is allowed here.
An upgrade needs explicit administrator confirmation; reinstall has a new key.
"""

import json
import re
from hashlib import sha256
from urllib.parse import urlsplit

from features.workbench import origin
from features.workbench.external import fail
from features.workbench.coordination import configuration
from core.db.models import ContentBlock, SystemConfig
from core.infra.time import utc_now
from core.capabilities.plugins.packaging.importer import normalize_admin_config as _normalize_admin_config, manifest_extensions
from core.capabilities.plugins.resources.crypto import encrypt_secret, decrypt_secret

PREFIX = "workbench_app_config:"
GROUP = "workbench_app_config"


def declaration(selected):
    try:
        manifest = json.loads((selected.package / "plugin.json").read_text())
        value = _normalize_admin_config(manifest, manifest_extensions(manifest))
    except (OSError, ValueError):
        fail(409, "configuration_declaration_invalid")
    if not value or len(value["fields"]) > 32:
        fail(404, "configuration_not_declared")
    fields = value["fields"]
    keys = [f["key"] for f in fields]
    if len(set(keys)) != len(keys) or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", key) for key in keys
    ):
        fail(409, "configuration_declaration_invalid")
    for field in fields:
        field["label"] = field["label"][:255]
        field["description"] = field["description"][:1024]
    value["group"] = value["group"][:128]
    value["hint"] = value["hint"][:1024]
    return value


def scope(db, key, selected):
    return {
        "applicationKey": key,
        "installId": selected.install_id,
        "revision": selected.revision,
        "origin": origin.current(db),
    }


def scope_id(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def row_key(identifier, field):
    return "wb_app:" + sha256((identifier + "\0" + field).encode()).hexdigest()


def read(db, key, selected):
    declared = declaration(selected)
    binding = scope(db, key, selected)
    identifier = scope_id(binding)
    marker = db.get(ContentBlock, PREFIX + identifier)
    values = {}
    if (
        marker is None
        or marker.payload.get("scope") != binding
        or marker.payload.get("administratorConfirmed") is not True
        or not marker.updated_by
        or marker.updated_by == "system_bootstrap"
    ):
        return declared, values
    for field in declared["fields"]:
        row = db.get(SystemConfig, row_key(identifier, field["key"]))
        if (
            row is None
            or row.group_key != GROUP
            or not row.updated_by
            or row.updated_by == "system_bootstrap"
        ):
            continue
        if row.is_secret:
            field["secret"] = True  # Durable confidentiality cannot be downgraded by metadata.
        value = decrypt_secret(row.config_value) if row.is_secret else row.config_value
        if value and value.strip():
            values[field["key"]] = value
    return declared, values


def view(db, item, selected, with_values=False):
    declared, values = read(db, item["applicationKey"], selected)
    fields = []
    for field in declared["fields"]:
        value = values.get(field["key"], "")
        field_view = {**field, "is_set": bool(value)}
        if with_values:
            field_view["value"] = ("****" if value else "") if field["secret"] else value
        fields.append(field_view)
    flags = [field["is_set"] for field in fields]
    return {
        **item,
        "fields": fields,
        "mode": declared["mode"],
        "group": declared["group"],
        "hint": declared["hint"],
        "configured": any(flags) if declared["mode"] == "any" else all(flags),
    }


def write(db, actor, key, revision, values, clear_keys):
    from features.workbench.application_config_bindings import resolve_admin

    configuration(db)
    db.expire_all()
    item, selected = resolve_admin(db, actor, key, revision)
    declared = declaration(selected)
    fields = {field["key"]: field for field in declared["fields"]}
    if not values and not clear_keys:
        fail(422, "configuration_values_required")
    if set(values) - fields.keys() or set(clear_keys) - fields.keys():
        fail(422, "configuration_field_unknown")
    if set(values) & set(clear_keys) or len(clear_keys) != len(set(clear_keys)):
        fail(422, "configuration_fields_conflict")
    if (
        any(len(value.encode()) > 4096 for value in values.values())
        or len(json.dumps(values).encode()) > 64 * 1024
    ):
        fail(413, "configuration_too_large")
    binding = scope(db, key, selected)
    identifier = scope_id(binding)
    now = utc_now()
    for name in set(values) | set(clear_keys):
        field = fields[name]
        row = db.get(SystemConfig, row_key(identifier, name))
        if name in values and field["secret"] and values[name] in ("", "****"):
            continue
        value = "" if name in clear_keys else values[name]
        secret = field["secret"] or bool(row and row.is_secret)
        if value and secret:
            try:
                value = encrypt_secret(value)
            except ValueError:
                fail(503, "configuration_encryption_unavailable")
        if row is None:
            row = SystemConfig(
                config_key=row_key(identifier, name),
                display_name=field["label"][:255],
                description=field["description"][:1024],
                group_key=GROUP,
                is_secret=secret,
            )
            db.add(row)
        row.config_value = value or None
        row.is_secret = secret  # Never downgrade a previously confidential row.
        row.updated_by = actor[:64]
        row.updated_at = now
    marker = db.get(ContentBlock, PREFIX + identifier)
    payload = {"scope": binding, "administratorConfirmed": True}
    if marker:
        marker.payload = payload
        marker.updated_by = actor[:64]
        marker.updated_at = now
    else:
        db.add(
            ContentBlock(
                id=PREFIX + identifier, payload=payload, updated_by=actor[:64], updated_at=now
            )
        )
    db.commit()
    from core.config.store.system_config import SystemConfigService

    SystemConfigService.get_instance().invalidate_cache()
    return view(db, item, selected, with_values=True)


def data_values(db, key, selected):
    # A source with no config declaration gets an empty explicit scope, never globals.
    try:
        _, values = read(db, key, selected)
        return values
    except Exception as error:
        from fastapi import HTTPException

        if isinstance(error, HTTPException) and error.status_code == 404:
            return {}
        raise


def trusted_origins(db, key, selected):
    try:
        declared, values = read(db, key, selected)
    except Exception as error:
        from fastapi import HTTPException

        if isinstance(error, HTTPException) and error.status_code == 404:
            return set()
        raise
    allowed = set()
    for field in declared["fields"]:
        if field["secret"]:
            continue
        raw = values.get(field["key"], "")
        try:
            parts = urlsplit(raw)
            if (
                parts.scheme in ("http", "https")
                and parts.hostname
                and not parts.username
                and not parts.password
                and "\\" not in raw
            ):
                port = parts.port or (443 if parts.scheme == "https" else 80)
                allowed.add((parts.scheme, parts.hostname.lower(), port))
        except ValueError:
            continue
    return allowed
