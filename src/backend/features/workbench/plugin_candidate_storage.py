"""Bound candidate retention; deletion touches only recorded upgrade-owned revisions."""

from hashlib import sha256
import json
import time

from features.workbench.external import fail
from features.workbench.coordination import configuration
from core.db.models import ContentBlock, InstalledPlugin
from features.desktop.models import DeviceCapabilityInstallation
from core.db.models.plugin_resource import PluginResource, PluginResourcePackage
from core.infra.time import utc_now
from core.harness.capability_errors import IntegrityFailed

PREFIX = "workbench_plugin_candidate:"
MAX_CANDIDATES = 2
MAX_BYTES = 100 * 1024 * 1024
MAX_OWNER_CANDIDATES = 8
MAX_OWNER_BYTES = 200 * 1024 * 1024
RESERVATION_SECONDS = 300


def records(db, binding=None):
    rows = db.query(ContentBlock).filter(ContentBlock.id.startswith(PREFIX, autoescape=True)).all()
    return [r for r in rows if binding is None or r.payload["binding"] == binding]


def identifier(binding, revision):
    return PREFIX + sha256((binding + ":" + revision).encode()).hexdigest()


def owner_key(user_id):
    return sha256(("global" if user_id is None else "user:" + user_id).encode()).hexdigest()


def live_bindings(db):
    from features.workbench.plugin_upgrades import binding_token

    rows = list(db.query(InstalledPlugin).all())
    rows.extend(
        db.query(DeviceCapabilityInstallation)
        .filter(
            DeviceCapabilityInstallation.kind == "plugin",
            DeviceCapabilityInstallation.state != "removed",
        )
        .all()
    )
    return {binding_token(r) for r in rows}


def protected(db):
    """Existing authoritative references win over candidate cleanup."""
    from features.workbench.plugin_upgrades import PREFIX as UPGRADES, HISTORY
    from features.workbench.plugin_revisions import LEASE

    active_bindings = live_bindings(db)
    keep = {
        (r.import_report or {}).get("package_revision") for r in db.query(InstalledPlugin).all()
    }
    keep.update(
        r.resolved_revision
        for r in db.query(DeviceCapabilityInstallation)
        .filter(DeviceCapabilityInstallation.state != "removed")
        .all()
    )
    keep.update(
        r.revision
        for r in db.query(PluginResource).filter(PluginResource.expires_at > time.time()).all()
    )
    for row in (
        db.query(ContentBlock).filter(ContentBlock.id.startswith(LEASE, autoescape=True)).all()
    ):
        if row.payload["expiresAt"] > time.time():
            keep.add(row.payload["revision"])
    for row in (
        db.query(ContentBlock).filter(ContentBlock.id.startswith(HISTORY, autoescape=True)).all()
    ):
        keep.add(row.payload["revision"])
    for row in (
        db.query(ContentBlock).filter(ContentBlock.id.startswith(UPGRADES, autoescape=True)).all()
    ):
        if row.payload.get("status") == "pending" and row.id[len(UPGRADES) :] in active_bindings:
            keep.add(row.payload.get("candidateRevision"))
    for row in records(db):
        if row.payload["binding"] not in active_bindings:
            continue
        if row.payload["status"] == "pending" or (
            row.payload["status"] == "reserved" and row.payload["expiresAt"] > time.time()
        ):
            keep.add(row.payload["revision"])
    return keep


def collect(db, binding=None, owner_scope=None):
    """Retry failed cleanup. Caller holds namespace and, for local files, management lock."""
    keep = protected(db)
    active_bindings = live_bindings(db)
    removed = 0
    for row in records(db, binding):
        value = row.payload
        if owner_scope and (value["owner"], value["scope"]) != owner_scope:
            continue
        if value["binding"] not in active_bindings:
            value = {**value, "status": "discarded"}
            row.payload = value
            from features.workbench.plugin_upgrades import PREFIX as UPGRADES

            orphan = db.get(ContentBlock, UPGRADES + value["binding"])
            if orphan is not None:
                db.delete(orphan)
        eligible = value["status"] == "discarded" or (
            value["status"] == "reserved" and value["expiresAt"] <= time.time()
        )
        if not eligible:
            continue
        if value["revision"] in keep:
            # Another current/history/live/pending owner keeps the bytes. Release only our claim.
            db.delete(row)
            removed += 1
            continue
        try:
            if value["scope"] == "local":
                from features.desktop.capabilities import store

                store.remove_revision("plugin", "local", value["key"], value["revision"])
            else:
                from core.capabilities.plugins.packaging.runtime_assets import forget_cache

                forget_cache(value["revision"])
                package = db.get(PluginResourcePackage, value["revision"])
                if package is not None:
                    db.delete(package)
            db.delete(row)
            removed += 1
        except (OSError, ValueError, IntegrityFailed) as error:
            # Keep ownership and quota charge; do not hide a permanently growing backlog.
            row.payload = {**value, "cleanupError": type(error).__name__}
    db.flush()
    return removed


def reserve(db, binding, revision, size, scope, key, owner=None):
    actor = owner_key(owner)
    collect(db, owner_scope=(actor, scope))
    row_id = identifier(binding, revision)
    existing = db.get(ContentBlock, row_id)
    owned = records(db, binding)
    count = len(owned) + (0 if existing else 1)
    total = sum(r.payload["sizeBytes"] for r in owned if r.id != row_id) + size
    actor_records = [r for r in records(db) if r.payload["owner"] == actor]
    actor_count = len(actor_records) + (0 if existing else 1)
    actor_bytes = sum(r.payload["sizeBytes"] for r in actor_records if r.id != row_id) + size
    if (
        count > MAX_CANDIDATES
        or total > MAX_BYTES
        or actor_count > MAX_OWNER_CANDIDATES
        or actor_bytes > MAX_OWNER_BYTES
    ):
        # Cleanup bookkeeping is safe to commit before rejecting an unwritten candidate.
        db.commit()
        fail(413, "candidate_storage_quota_exceeded")
    if existing:
        existing.payload = {**existing.payload, "expiresAt": time.time() + RESERVATION_SECONDS}
    else:
        db.add(
            ContentBlock(
                id=row_id,
                payload={
                    "binding": binding,
                    "owner": actor,
                    "revision": revision,
                    "scope": scope,
                    "key": key,
                    "sizeBytes": size,
                    "status": "reserved",
                    "expiresAt": time.time() + RESERVATION_SECONDS,
                },
                updated_at=utc_now(),
            )
        )
    db.flush()


def change(db, binding, revision, status):
    row = db.get(ContentBlock, identifier(binding, revision))
    if row:
        if status == "current":
            # InstalledPlugin/registry now own the bytes, not the candidate queue.
            db.delete(row)
        else:
            row.payload = {**row.payload, "status": status}
    db.flush()


def replace(db, binding, previous, revision):
    if previous and previous != revision:
        change(db, binding, previous, "discarded")
    change(db, binding, revision, "pending")


def local_reserve(inst, revision, files):
    """Reserve before any candidate/skill directory is written; existing file lock is held."""
    from features.desktop.capabilities import registry
    from features.workbench.plugin_upgrades import binding_token

    size = sum(len(v if isinstance(v, bytes) else v.encode()) for v in files.values())
    if size > 50 * 1024 * 1024:
        fail(413, "candidate_storage_quota_exceeded")
    size += len(json.dumps({"files": sorted(files)}, ensure_ascii=False, indent=2).encode())
    with registry._session() as db:
        configuration(db)
        reserve(
            db,
            binding_token(inst),
            revision,
            size,
            "local",
            inst.key,
            inst.payload.get("owner_user_id"),
        )


def janitor(db):
    """Avoid deleting local prepared files while a management mutation holds the file lock."""
    from core.harness.registry.run_capabilities import has_run_capabilities

    if not has_run_capabilities():
        configuration(db)
        return collect(db)
    from features.desktop.capabilities.paths import meta_root
    from core.infra.file_lock import locked, LockTimeout

    try:
        with locked(meta_root() / "management.lock", timeout=0.1):
            configuration(db)
            return collect(db)
    except LockTimeout:
        return 0


def summary(db, binding, owner=None):
    owned = records(db, binding)
    actor_records = [r for r in records(db) if r.payload["owner"] == owner_key(owner)]
    return {
        "ownerCount": len(actor_records),
        "ownerBytes": sum(r.payload["sizeBytes"] for r in actor_records),
        "ownerLimitCount": MAX_OWNER_CANDIDATES,
        "ownerLimitBytes": MAX_OWNER_BYTES,
        "count": len(owned),
        "bytes": sum(r.payload["sizeBytes"] for r in owned),
        "limitCount": MAX_CANDIDATES,
        "limitBytes": MAX_BYTES,
        "cleanupPending": sum(r.payload["status"] == "discarded" for r in owned),
        "cleanupFailures": sum(bool(r.payload.get("cleanupError")) for r in owned),
    }
