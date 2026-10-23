"""Versioned content-v1 adapter after WB-20 cutover; canonical rows are the only source."""

from features.workbench.coordination import configuration


from copy import deepcopy

from features.workbench import external
from features.workbench.migration_snapshot import RECEIPT
from core.db.models import ContentBlock
from core.infra.time import utc_now


def active(db):
    row = db.get(ContentBlock, RECEIPT)
    return bool(row and row.payload.get("status") == "applied")


def app_config(db):
    if not active(db):
        from core.files.content.content_blocks_constants import DEFAULT_APP_CONFIG
        from core.files.content.branding import normalize_app_config

        row = db.get(ContentBlock, "app_config")
        return normalize_app_config(row.payload if row else DEFAULT_APP_CONFIG)
    values = []
    for item in external.items(db):
        if not item.get("legacy"):
            continue
        declaration = item["declaration"]
        values.append(
            {
                "id": declaration["id"],
                "name": declaration["title"],
                "description": declaration.get("description", ""),
                "url": declaration["target"]["url"],
                "icon": item.get("legacyIcon", ""),
                "enabled": item["enabled"],
            }
        )
    return {"apps": values}


def version(db):
    if not active(db):
        row = db.get(ContentBlock, "app_config")
        return row.updated_at.isoformat() if row and row.updated_at else None
    rows = (
        db.query(ContentBlock)
        .filter(ContentBlock.id.startswith(external.PREFIX, autoescape=True))
        .all()
    )
    return max((row.updated_at.isoformat() for row in rows if row.updated_at), default=None)


def page_config(db, payload):
    result = deepcopy(payload)
    if not active(db):
        return result
    from features.workbench import policy

    layout = policy.read(db)
    nav = result.setdefault("navigation", {})
    nav["sidebar_items"] = [
        key[8:] for key in layout["defaultPinned"] if key.startswith("builtin:")
    ]
    nav["menu_items"] = [
        key[8:] for key in layout.get("legacyMenu", []) if key.startswith("builtin:")
    ]
    return result


def stage_write(db, alias, payload):
    """Return True for fully-handled app writes; all writes remain in caller transaction."""
    if not active(db):
        return False
    configuration(db)
    if alias == "page_config":
        navigation = payload.get("navigation", {})
        if isinstance(navigation, dict) and ({"sidebar_items", "menu_items"} & set(navigation)):
            from features.workbench import policy
            from features.workbench.catalog import BUILTINS

            layout = policy.read(db)
            for field in ("sidebar_items", "menu_items"):
                values = navigation.get(field)
                if values is None:
                    continue
                if (
                    not isinstance(values, list)
                    or any(not isinstance(value, str) for value in values)
                    or len(values) != len(set(values))
                    or any(value not in BUILTINS for value in values)
                ):
                    external.fail(422, "invalid_legacy_navigation")
                keys = ["builtin:" + value for value in values]
                if field == "sidebar_items":
                    fixed = {"chat", "settings", "docs", "plan_mode"}
                    layout["defaultPinned"] = [key for key in keys if key[8:] not in fixed]
                else:
                    layout["legacyMenu"] = keys
                navigation.pop(field)
            layout["revision"] += 1
            _put(db, "workbench_policy", layout)
        return False
    if alias != "app_config":
        return False
    from features.workbench.migration_plan import plan

    if (
        not isinstance(payload, dict)
        or set(payload) != {"apps"}
        or not isinstance(payload["apps"], list)
    ):
        external.fail(422, "invalid_legacy_app_config")
    source = {"blocks": {"app_config": payload}, "users": {}, "plugins": {}, "permissions": {}}
    # All IDs are checked before mutation. Existing independent external IDs cannot be claimed.
    for item in external.items(db):
        if not item.get("legacy"):
            source["blocks"][external.PREFIX + item["declaration"]["id"]] = item
    conversion = plan(source)
    if conversion["report"]["quarantined"]:
        external.fail(422, "invalid_legacy_app_config")
    incoming = conversion["external"]
    for item in external.items(db):
        if item.get("legacy") and external.PREFIX + item["declaration"]["id"] not in incoming:
            db.delete(db.get(ContentBlock, external.PREFIX + item["declaration"]["id"]))
    for key, item in incoming.items():
        old = db.get(ContentBlock, key)
        item["revision"] = old.payload["revision"] + 1 if old else 1
        _put(db, key, item)
    return True


def _put(db, key, value):
    row = db.get(ContentBlock, key)
    if row:
        row.payload = deepcopy(value)
        row.updated_at = utc_now()
    else:
        db.add(ContentBlock(id=key, payload=deepcopy(value), updated_at=utc_now()))


def build_docs_snapshot(db):
    from core.files.content.content_blocks import build_docs_snapshot as original

    result = original(db)
    if active(db):
        result["blocks"]["app_config"]["payload"] = app_config(db)
        result["blocks"]["app_config"]["updated_at"] = version(db)
        result["blocks"]["page_config"]["payload"] = page_config(
            db, result["blocks"]["page_config"]["payload"]
        )
    return result


def import_docs_snapshot(db, snapshot, *, overwrite=True, default_updated_by=None):
    from core.files.content.content_blocks_constants import DOCS_BLOCK_MAP
    from core.files.content.content_blocks import import_docs_snapshot as original
    from core.files.content.content_blocks import normalize_docs_snapshot

    if not active(db):
        return original(db, snapshot, overwrite=overwrite, default_updated_by=default_updated_by)
    normalized = normalize_docs_snapshot(snapshot)
    imported, skipped = [], []
    try:
        configuration(db)
        for alias, block in normalized["blocks"].items():
            key = DOCS_BLOCK_MAP[alias]
            row = db.get(ContentBlock, key)
            exists = row is not None or alias == "app_config"
            if exists and not overwrite:
                skipped.append(alias)
                continue
            value = deepcopy(block["payload"])
            if not stage_write(db, alias, value):
                _put(db, key, value)
            imported.append(alias)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "schema_version": 1,
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "overwrite": overwrite,
    }
