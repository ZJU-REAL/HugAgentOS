"""Server-owned approvals for exact installations, users and environments."""

from features.workbench.coordination import runtime


from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from features.workbench import catalog, external
from core.db.models import ContentBlock
from core.infra.time import as_utc, utc_now
from features.workbench.installation_binding import exact as resolve

SUPPORTED = {
    "data.query",
    "file.pick",
    "file.save",
    "chat.compose",
    "navigation.open",
    "platform.openExternal",
}


def binding(db, user_id, key, revision, module_id=None, instance_id=None):
    db.expire_all()
    item = catalog.readable(db, user_id, key)
    if item["declaration"]["target"]["kind"] != "plugin":
        external.fail(404, "app_not_found")
    if not item["availability"]["available"]:
        external.fail(409, "app_disabled")
    current = item["binding"]
    selected = resolve(db, current["slug"], user_id, current["installId"])
    if selected.ui.get("version") == 3 and not instance_id:
        external.fail(403, "instance_binding_mismatch")
    if instance_id or revision != current["packageRevision"]:
        from features.workbench.plugin_revisions import resolve_revision

        selected = resolve_revision(db, user_id, key, revision, instance_id)
        current = {**current, "packageRevision": revision}
    target = item["declaration"]["target"]
    allowed = {target["moduleId"]} | {
        view["moduleId"] for view in item["declaration"].get("views", []) if view.get("moduleId")
    }
    module_id = module_id or target["moduleId"]
    if module_id not in allowed:
        external.fail(403, "module_unavailable")
    module = next((m for m in selected.ui["contributes"]["modules"] if m["id"] == module_id), None)
    if module is None:
        external.fail(409, "module_unavailable")
    return current, selected, set(module.get("grants", []))


def record_id(token):
    return "workbench_approval:" + sha256(token.encode()).hexdigest()


def approve(db, user_id, key, revision, capabilities, module_id=None, instance_id=None):
    runtime(db, user_id)
    current, _, requested = binding(db, user_id, key, revision, module_id, instance_id)
    if len(capabilities) != len(set(capabilities)) or not set(capabilities) <= requested:
        external.fail(403, "capability_not_requested")
    approved = set(capabilities)
    if any(c not in SUPPORTED and not c.startswith("data_source:") for c in approved):
        external.fail(403, "unsupported_capability")
    token = token_urlsafe(32)
    now = utc_now()
    value = {
        "instanceId": instance_id,
        "moduleId": module_id
        or catalog.readable(db, user_id, key)["declaration"]["target"]["moduleId"],
        "userId": user_id,
        "applicationKey": key,
        "binding": current,
        "requested": sorted(requested),
        "approved": sorted(approved),
        "expiresAt": (now + timedelta(minutes=15)).isoformat(),
        "revoked": False,
        "window": int(now.timestamp()) // 60,
        "calls": 0,
        "leases": {},
        "files": [],
    }
    db.add(ContentBlock(id=record_id(token), payload=value, updated_by=user_id, updated_at=now))
    db.commit()
    return {
        "approvalId": token,
        "requested": sorted(requested),
        "approved": sorted(approved),
        "effective": sorted(approved),
        "expiresAt": value["expiresAt"],
    }


def authorized(db, user_id, key, revision, token, capability=None, instance_id=None):
    row = db.get(ContentBlock, record_id(token))
    value = deepcopy(row.payload) if row else {}
    if (
        value.get("userId") == user_id
        and value.get("applicationKey") == key
        and value.get("binding", {}).get("packageRevision") != revision
    ):
        external.fail(409, "revision_conflict")
    current, selected, requested = binding(
        db, user_id, key, revision, value.get("moduleId"), value.get("instanceId")
    )
    if (
        value.get("instanceId") != instance_id
        or value.get("userId") != user_id
        or value.get("applicationKey") != key
        or value.get("binding") != current
        or value.get("revoked")
        or not value.get("expiresAt")
        or as_utc(datetime.fromisoformat(value["expiresAt"])) <= utc_now()
    ):
        external.fail(403, "approval_invalid")
    effective = requested & set(value["approved"])
    if capability and capability not in effective:
        external.fail(403, "capability_denied")
    return row, value, selected, effective


def revoke_matching(db, user_id, keys=None, revision=None, instance_id=None):
    """Stage revocation in the caller's transaction, including unbound old grants."""
    rows = (
        db.query(ContentBlock)
        .filter(
            ContentBlock.id.startswith("workbench_approval:", autoescape=True),
            ContentBlock.payload["userId"].as_string() == user_id,
        )
        .all()
    )
    for row in rows:
        value = deepcopy(row.payload)
        if (
            keys is not None
            and value.get("applicationKey") not in keys
            or revision is not None
            and value.get("binding", {}).get("packageRevision") != revision
            or instance_id is not None
            and value.get("instanceId") != instance_id
        ):
            continue
        row.payload = {**value, "revoked": True, "leases": {}}


def revoke(db, user_id, key, revision, token):
    runtime(db, user_id)
    db.expire_all()
    row = db.get(ContentBlock, record_id(token))
    value = deepcopy(row.payload) if row else {}
    if (
        value.get("userId") != user_id
        or value.get("applicationKey") != key
        or value.get("binding", {}).get("packageRevision") != revision
    ):
        external.fail(403, "approval_invalid")
    # Cleanup stays possible after instance close/expiry without restoring authority.
    row.payload = {**value, "revoked": True, "leases": {}}
    db.commit()


def reserve(db, user_id, key, revision, token, capability, instance_id=None):
    runtime(db, user_id)
    row, value, selected, effective = authorized(
        db, user_id, key, revision, token, capability, instance_id
    )
    now = utc_now().timestamp()
    budget_id = "workbench_budget:" + sha256((user_id + "\0" + key).encode()).hexdigest()
    budget_row = db.get(ContentBlock, budget_id)
    budget = deepcopy(budget_row.payload) if budget_row else {"leases": {}, "window": 0, "calls": 0}
    leases = {k: v for k, v in budget["leases"].items() if v > now}
    window = int(now) // 60
    calls = budget["calls"] if budget["window"] == window else 0
    if len(leases) >= 4 or calls >= 30:
        db.rollback()
        external.fail(429, "capability_limit")
    lease = token_urlsafe(16)
    leases[lease] = now + 35
    updated = {"leases": leases, "calls": calls + 1, "window": window}
    if budget_row:
        budget_row.payload = updated
    else:
        db.add(
            ContentBlock(id=budget_id, payload=updated, updated_by=user_id, updated_at=utc_now())
        )
    row.payload = {**value, "budgetId": budget_id}
    db.commit()
    return lease, selected, effective


def release(db, token, lease):
    owner = db.get(ContentBlock, record_id(token))
    if owner is None:
        return
    runtime(db, owner.payload["userId"])
    db.expire_all()
    row = db.get(ContentBlock, record_id(token))
    if row:
        value = deepcopy(row.payload)
        budget_row = db.get(ContentBlock, value.get("budgetId", ""))
        if budget_row:
            budget = deepcopy(budget_row.payload)
            budget["leases"].pop(lease, None)
            budget_row.payload = budget
    db.commit()


def select_file(
    db, user_id, key, revision, token, file_id, capability="file.pick", instance_id=None
):
    runtime(db, user_id)
    row, value, _, _ = authorized(db, user_id, key, revision, token, capability, instance_id)
    artifact = readable_file(db, user_id, file_id)
    if file_id not in value["files"]:
        if len(value["files"]) >= 100:
            external.fail(429, "file_selection_limit")
        value["files"].append(file_id)
        row.payload = value
        db.commit()
    return artifact


def readable_file(db, user_id, file_id):
    from core.auth.edition import resolve_artifact_access
    from core.db.models import Artifact
    from core.files.edition import artifact_access_metadata

    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == file_id, Artifact.deleted_at.is_(None))
        .populate_existing()
        .first()
    )
    metadata = artifact_access_metadata(artifact) if artifact else {}
    access = (
        resolve_artifact_access(db, user_id, metadata.get("owner_id"), metadata.get("scope_id"))
        if artifact
        else "none"
    )
    if access == "none":
        external.fail(404, "file_not_found")
    return artifact


def configured_hosts(db, selected, key=None):
    """No global manifest-key trust. Legacy callers without exact identity get none."""
    if key is None:
        return set()
    from features.workbench.application_config import trusted_origins

    return {host for _, host, _ in trusted_origins(db, key, selected)}
