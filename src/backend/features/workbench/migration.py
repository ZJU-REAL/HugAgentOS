"""WB-20 operator boundary. Explicit freeze, protected snapshot, atomic cutover."""

from features.workbench.coordination import configuration


from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from features.workbench import external
from features.workbench.migration_plan import plan
from features.workbench.migration_snapshot import RECEIPT, digest, read_snapshot, state, write_snapshot
from core.db.models import ContentBlock, UserShadow
from core.infra.time import utc_now


def inventory(db, snapshot_path):
    source = state(db)
    snapshot = {"schemaVersion": 1, "exportedAt": utc_now().isoformat(), "source": source}
    checksum = write_snapshot(snapshot_path, snapshot)
    return {**plan(source)["report"], "digest": checksum}


def put(db, key, payload):
    row = db.get(ContentBlock, key)
    if row:
        row.payload = deepcopy(payload)
        row.updated_at = utc_now()
        row.updated_by = "workbench_migration"
    else:
        db.add(ContentBlock(id=key, payload=deepcopy(payload), updated_by="workbench_migration"))


def verified(path, expected):
    value = read_snapshot(path)
    if digest(value) != expected or value.get("schemaVersion") != 1:
        raise ValueError("snapshot_digest_mismatch")
    return value


def apply(db, snapshot_path, expected_digest, *, writes_frozen=False):
    if not writes_frozen:
        raise ValueError("writes_not_frozen")
    snapshot = verified(snapshot_path, expected_digest)
    try:
        configuration(db)
        db.expire_all()
        existing = db.get(ContentBlock, RECEIPT)
        previous = deepcopy(existing.payload) if existing else None
        if previous and previous["status"] == "applied":
            if previous["sourceDigest"] != expected_digest:
                raise ValueError("migration_receipt_conflict")
            return previous
        if previous and previous["sourceDigest"] == expected_digest:
            raise ValueError("migration_new_inventory_required")
        source = state(db)
        if digest(source) != digest(snapshot["source"]):
            raise ValueError("source_changed")
        conversion = plan(source)
        if conversion["report"]["authGaps"]:
            raise ValueError("migration_auth_incomplete")
        if previous:
            put(db, "workbench_migration_history:" + uuid4().hex, previous)
        for key, value in conversion["external"].items():
            put(db, key, value)
        put(db, "workbench_policy", conversion["policy"])
        put(db, "page_config", conversion["page"])
        for key, value in conversion["navigation"].items():
            put(db, key, value)
        for user_id, metadata in conversion["users"].items():
            db.get(UserShadow, user_id).extra_data = deepcopy(metadata)
        original = db.get(ContentBlock, "app_config")
        if original:
            db.delete(original)
        put(db, "workbench_migration_quarantine", conversion["report"])
        db.flush()
        after = {"schemaVersion": 1, "source": state(db)}
        after_path = Path(snapshot_path).with_name(
            Path(snapshot_path).name + "." + uuid4().hex + ".after"
        )
        after_hash = write_snapshot(after_path, after)
        receipt = {
            "schemaVersion": 1,
            "revision": previous["revision"] + 1 if previous else 1,
            "status": "applied",
            "sourceDigest": expected_digest,
            "afterDigest": after_hash,
            "afterSnapshot": after_path.name,
            "compatibility": "content-v1",
            "quarantined": conversion["report"]["quarantined"],
            "authGaps": conversion["report"]["authGaps"],
        }
        put(db, RECEIPT, receipt)
        db.commit()
        db.expire_all()
        return receipt
    except Exception:
        db.rollback()
        raise
    finally:
        # Also releases a lock acquired by the idempotent read path.
        db.rollback()


def delta_between(before, after):
    return {
        key: deepcopy(after.get(key))
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    }


def legacy_delta(old, after, current):
    """Replay only modifications to already-mapped ordinary legacy records.

    New identities, modern state, policies, auth changes and plugin changes cannot
    safely downgrade. Keep the newer version when any such delta exists.
    """
    delta = delta_between(after["blocks"], current["blocks"])
    blockers = []
    replay = deepcopy(old["blocks"].get("app_config", {"apps": []}))
    for key, value in delta.items():
        if key.startswith(external.AUDIT):
            continue  # audit remains append-only on downgrade
        previous = after["blocks"].get(key, {})
        if not key.startswith(external.PREFIX) or not previous.get("legacy"):
            blockers.append(key)
            continue
        identifier = previous["declaration"]["id"]
        replacement = None
        if value is not None:
            declaration = value.get("declaration", {})
            target = declaration.get("target", {})
            if (
                value.get("applicationKey") != "legacy:" + identifier
                or declaration.get("id") != identifier
                or set(declaration) - {"id", "title", "schemaVersion", "description", "target"}
                or target.get("kind") != "external"
                or target.get("mode") != "browser"
            ):
                blockers.append(key)
                continue
            replacement = {
                "id": identifier,
                "enabled": value["enabled"],
                "name": declaration["title"],
                "description": declaration.get("description", ""),
                "url": target["url"],
                "icon": value.get("legacyIcon", ""),
            }
        replay["apps"] = [
            replacement if isinstance(app, dict) and app.get("id") == identifier else app
            for app in replay.get("apps", [])
            if not isinstance(app, dict) or app.get("id") != identifier or replacement
        ]
    if any(after[k] != current[k] for k in ("users", "plugins", "permissions")):
        blockers.append("identity_or_plugin_delta")
    return delta, blockers, replay


def rollback(db, snapshot_path, expected_digest, delta_path, *, writes_frozen=False):
    if not writes_frozen:
        raise ValueError("writes_not_frozen")
    snapshot = verified(snapshot_path, expected_digest)
    try:
        configuration(db)
        db.expire_all()
        row = db.get(ContentBlock, RECEIPT)
        if not row or row.payload.get("sourceDigest") != expected_digest:
            raise ValueError("migration_receipt_conflict")
        receipt = deepcopy(row.payload)
        if receipt["status"] == "rolled_back":
            return receipt
        name = receipt["afterSnapshot"]
        if Path(name).name != name:
            raise ValueError("unsafe_after_snapshot")
        after = verified(Path(snapshot_path).with_name(name), receipt["afterDigest"])["source"]
        current = state(db)
        old = snapshot["source"]
        delta, blockers, replay = legacy_delta(old, after, current)
        exported = {
            "schemaVersion": 1,
            "delta": delta,
            "users": delta_between(after["users"], current["users"]),
            "plugins": delta_between(after["plugins"], current["plugins"]),
            "permissions": (
                current["permissions"] if after["permissions"] != current["permissions"] else {}
            ),
            "sourceDigest": expected_digest,
            "blockers": blockers,
        }
        write_snapshot(delta_path, exported)
        if blockers:
            raise ValueError("delta_not_representable")
        for key in set(old["blocks"]) | set(after["blocks"]):
            # Only restore rows changed by this migration, never arbitrary history.
            if old["blocks"].get(key) == after["blocks"].get(key):
                continue
            if key in old["blocks"]:
                put(db, key, replay if key == "app_config" else old["blocks"][key])
            else:
                record = db.get(ContentBlock, key)
                if record:
                    db.delete(record)
        for user_id, metadata in old["users"].items():
            if metadata != after["users"].get(user_id):
                db.get(UserShadow, user_id).extra_data = deepcopy(metadata)
        receipt.update(
            status="rolled_back", revision=receipt["revision"] + 1, deltaDigest=digest(exported)
        )
        put(db, RECEIPT, receipt)
        db.commit()
        db.expire_all()
        return receipt
    except Exception:
        db.rollback()
        raise
    finally:
        db.rollback()


def clone_origin(db, snapshot_path, expected_digest, *, writes_frozen=False):
    """Rotate copied service identity without granting or remapping old plugin keys."""
    if not writes_frozen:
        raise ValueError("writes_not_frozen")
    snapshot = verified(snapshot_path, expected_digest)
    try:
        configuration(db)
        db.expire_all()
        if digest(state(db)) != digest(snapshot["source"]):
            raise ValueError("source_changed")
        put(db, "workbench_origin_identity", {"id": uuid4().hex})
        db.commit()
        return {"status": "origin_rotated", "oldPluginGrants": "not_remapped"}
    except Exception:
        db.rollback()
        raise
