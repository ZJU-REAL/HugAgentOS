"""Safe UI snapshots in the existing durable ContentBlock store.

The workbench_application_state namespace is the logical state entity. Reusing
ContentBlock avoids DDL and preserves SQLite/CE installations. Its owner lock
serializes CAS and aggregate quotas in one transaction on SQLite and PostgreSQL.
"""

from features.workbench.coordination import runtime


import json
import os
import re
from copy import deepcopy
from hashlib import sha256
from features.workbench import catalog, external
from core.db.models import ContentBlock
from core.infra.time import utc_now
from sqlalchemy import update

PREFIX = "workbench_application_state:"
SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|credential|authorization|cookie|token|api.?key|private.?key)", re.I
)
SECRET_VALUE = re.compile(
    r"(?:^Bearer\s|-----BEGIN .*PRIVATE KEY|[?&#](?:token|sso_token|access_token|password|api_key)=)",
    re.I,
)


def limit(name, default, maximum):
    try:
        return max(1, min(int(os.environ.get(name, default)), maximum))
    except ValueError:
        return default


def encode(payload):
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode()) > limit("WORKBENCH_STATE_APP_BYTES", 256 * 1024, 1024 * 1024):
            external.fail(413, "state_quota_exceeded")
        visit(payload)
        return raw
    except (ValueError, TypeError, RecursionError):
        external.fail(422, "invalid_state")


def visit(value, depth=0):
    if depth > 32:
        external.fail(422, "invalid_state")
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(key):
                external.fail(422, "state_contains_credentials")
            visit(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            visit(child, depth + 1)
    elif isinstance(value, str) and SECRET_VALUE.search(value):
        external.fail(422, "state_contains_credentials")


def scope(
    db, user_id, key, package_revision, install_id, workspace, origin=None, execution_scope=None
):
    item = catalog.readable(db, user_id, key)
    if not item["availability"]["available"]:
        external.fail(409, item["availability"]["reason"] or "app_disabled")
    binding = item.get("binding", {})
    expected_install = binding.get("installId", "host:" + key)
    if (
        str(item["revision"]) != package_revision
        or (binding and install_id != expected_install)
        or (not binding and install_id not in (None, expected_install))
    ):
        external.fail(409, "revision_conflict")
    expected_origin = binding.get("origin")
    if isinstance(expected_origin, dict):
        expected_origin = expected_origin.get("id")
    expected_scope = binding.get("executionScope", "host")
    if binding and (origin != expected_origin or execution_scope != expected_scope):
        external.fail(409, "origin_unavailable")
    if not binding and (origin is not None or execution_scope not in (None, "host")):
        external.fail(409, "origin_unavailable")
    identity = [key, expected_install, expected_origin, expected_scope, workspace]
    owner = PREFIX
    app = sha256(json.dumps(identity[:4]).encode()).hexdigest()
    row_id = owner + sha256((user_id + "\0" + json.dumps(identity)).encode()).hexdigest()
    return owner, app, row_id, item


def read(
    db,
    user_id,
    key,
    package_revision,
    install_id,
    workspace,
    schema_version=None,
    origin=None,
    execution_scope=None,
):
    _, _, row_id, _ = scope(
        db, user_id, key, package_revision, install_id, workspace, origin, execution_scope
    )
    row = db.get(ContentBlock, row_id)
    if not row:
        return {
            "revision": 0,
            "schemaVersion": schema_version,
            "payload": None,
            "resetRequired": False,
        }
    value = row.payload
    mismatch = schema_version is not None and schema_version != value["schemaVersion"]
    return {
        "revision": value["revision"],
        "schemaVersion": value["schemaVersion"],
        "payload": None if mismatch else deepcopy(value["payload"]),
        "resetRequired": mismatch,
    }


def save(
    db,
    user_id,
    key,
    package_revision,
    install_id,
    workspace,
    body,
    origin=None,
    execution_scope=None,
):
    raw = encode(body["payload"])
    # Bootstrap the durable service identity before acquiring the write barrier.
    # origin.current may commit on first use; it must never release our CAS lock.
    from features.workbench import origin as service_origin

    service_origin.current(db)
    try:
        runtime(db, user_id)
        # Resolve after the transaction lock; the client-supplied binding never grants access.
        owner, app, row_id, item = scope(
            db, user_id, key, package_revision, install_id, workspace, origin, execution_scope
        )
        declared = item["declaration"].get("lifecycle", {}).get("stateSchemaVersion")
        if declared is not None and body["schemaVersion"] != declared:
            external.fail(409, "state_schema_mismatch")
        row = db.get(ContentBlock, row_id, populate_existing=True)
        current = row.payload if row else {"revision": 0}
        if body["revision"] != current["revision"]:
            external.fail(409, "revision_conflict")
        if row and body["schemaVersion"] != current["schemaVersion"] and not body["reset"]:
            external.fail(409, "state_reset_required")
        rows = (
            db.query(ContentBlock)
            .filter(
                ContentBlock.id.startswith(owner, autoescape=True),
                ContentBlock.updated_by == user_id,
            )
            .all()
        )
        size = len(raw.encode())
        others = [r.payload for r in rows if r.id != row_id]
        total = size + sum(v["sizeBytes"] for v in others)
        per_app = size + sum(v["sizeBytes"] for v in others if v["application"] == app)
        if total > limit(
            "WORKBENCH_STATE_USER_BYTES", 10 * 1024 * 1024, 100 * 1024 * 1024
        ) or per_app > limit("WORKBENCH_STATE_APP_BYTES", 256 * 1024, 1024 * 1024):
            external.fail(413, "state_quota_exceeded")
        value = {
            "revision": current["revision"] + 1,
            "schemaVersion": body["schemaVersion"],
            "payload": body["payload"],
            "application": app,
            "sizeBytes": size,
        }
        if row and body["reset"] and body["schemaVersion"] != current["schemaVersion"]:
            from features.workbench.state_recovery import preserve_before_reset

            preserve_before_reset(db, row)
        now = utc_now()
        if not row:
            db.add(ContentBlock(id=row_id, payload=value, updated_by=user_id, updated_at=now))
        else:
            count = db.execute(
                update(ContentBlock)
                .where(
                    ContentBlock.id == row_id,
                    ContentBlock.payload["revision"].as_integer() == current["revision"],
                )
                .values(payload=value, updated_by=user_id, updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if count != 1:
                external.fail(409, "revision_conflict")
        db.commit()
        db.expire_all()
        return {
            "revision": value["revision"],
            "schemaVersion": value["schemaVersion"],
            "payload": deepcopy(value["payload"]),
            "resetRequired": False,
        }
    except Exception:
        db.rollback()
        raise
