"""Bounded, durable activity projection; reads never load a page or start a worker."""

from features.workbench.coordination import runtime


import re
import time
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from features.workbench import catalog, external
from core.db.models import ContentBlock
from core.infra.time import utc_now

CAPABILITIES = {"badge", "notification", "background"}
INSTANCE_TTL = 90
BADGE_TTL = 3600


def identity(user_id):
    return "workbench_activity:" + sha256(user_id.encode()).hexdigest()


def read(db, user_id):
    row = db.get(ContentBlock, identity(user_id))
    return (
        deepcopy(row.payload)
        if row
        else {"instances": {}, "grants": {}, "badges": {}, "notifications": [], "background": {}}
    )


def save(db, user_id, value):
    key = identity(user_id)
    row = db.get(ContentBlock, key)
    if row is None:
        row = ContentBlock(id=key)
        db.add(row)
    row.payload = deepcopy(value)
    row.updated_at = utc_now()
    row.updated_by = user_id
    db.commit()


def binding(item):
    return {"revision": str(item["revision"]), **item.get("binding", {})}


def available(db, user_id, key):
    item = catalog.readable(db, user_id, key)
    if not item["availability"]["available"]:
        external.fail(403, "permission_denied")
    return item


def prune(value, now):
    value["instances"] = {k: v for k, v in value["instances"].items() if v["expiresAt"] > now}
    value["badges"] = {k: v for k, v in value["badges"].items() if v["expiresAt"] > now}
    value["notifications"] = [v for v in value["notifications"] if v["expiresAt"] > now][-20:]


def register(db, user_id, key, instance_id, expected, module_id=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", instance_id):
        external.fail(422, "invalid_instance_id")
    item = available(db, user_id, key)
    module_id = module_id or item["declaration"]["target"].get("moduleId")
    allowed_modules = {item["declaration"]["target"].get("moduleId")} | {
        v.get("moduleId") for v in item["declaration"].get("views", [])
    }
    if module_id not in allowed_modules:
        external.fail(403, "module_not_declared")
    if expected != binding(item):
        external.fail(409, "revision_conflict")
    runtime(db, user_id)
    db.expire_all()
    value = read(db, user_id)
    now = time.time()
    prune(value, now)
    previous = value["instances"].get(instance_id)
    if previous and (previous["applicationKey"] != key or previous["binding"] != expected):
        external.fail(409, "instance_binding_mismatch")
    if not previous and len(value["instances"]) >= 8:
        external.fail(429, "instance_quota_exhausted")
    value["instances"][instance_id] = {
        "applicationKey": key,
        "binding": expected,
        "moduleId": module_id,
        "expiresAt": now + INSTANCE_TTL,
        "rates": previous["rates"] if previous else {},
    }
    save(db, user_id, value)
    return {"instanceId": instance_id, "expiresAt": now + INSTANCE_TTL}


def declared(item, db, user_id, module_id=None):
    if item["declaration"]["target"]["kind"] != "plugin":
        return set()
    from core.capabilities.plugins.resources.installation import resolve

    selected = resolve(db, item["binding"]["slug"], user_id, item["binding"]["installId"])
    module = next(
        (
            m
            for m in selected.ui.get("contributes", {}).get("modules", [])
            if m.get("id") == (module_id or item["declaration"]["target"]["moduleId"])
        ),
        {},
    )
    methods = {
        "badge": "badge.update",
        "notification": "notification.show",
        "background": "background",
    }
    return {
        c
        for c in item["declaration"].get("permissions", [])
        if c in methods and methods[c] in module.get("grants", [])
    }


def authorize(db, user_id, key, instance_id, capability=None, module_id=None):
    value = read(db, user_id)
    row = value["instances"].get(instance_id)
    if not row or row["applicationKey"] != key:
        external.fail(404, "instance_not_found")
    if row["expiresAt"] <= time.time():
        external.fail(410, "instance_expired")
    item = available(db, user_id, key)
    if capability:
        actual_module = module_id or item["declaration"]["target"].get("moduleId")
        if actual_module != row.get("moduleId"):
            external.fail(409, "module_changed")
    if binding(item) != row["binding"]:
        external.fail(409, "revision_conflict")
    if capability:
        grant = value["grants"].get(key)
        if (
            not grant
            or grant["binding"] != row["binding"]
            or capability not in grant["capabilities"]
            or capability not in declared(item, db, user_id, row.get("moduleId"))
        ):
            external.fail(403, "capability_not_granted")
    return value, row, item


def consent(db, user_id, key, instance_id, capabilities):
    # Called only by a host-rendered consent control, never a bridge handler.
    runtime(db, user_id)
    db.expire_all()
    value, row, item = authorize(db, user_id, key, instance_id)
    if set(capabilities) - CAPABILITIES or set(capabilities) - declared(
        item, db, user_id, row.get("moduleId")
    ):
        external.fail(403, "capability_not_declared")
    if capabilities and key not in value["grants"] and len(value["grants"]) >= 100:
        external.fail(429, "activity_grant_quota_exhausted")
    if capabilities:
        value["grants"][key] = {"binding": row["binding"], "capabilities": capabilities}
    else:
        value["grants"].pop(key, None)
    if "badge" not in capabilities:
        value["badges"].pop(key, None)
    if "notification" not in capabilities:
        value["notifications"] = [n for n in value["notifications"] if n["applicationKey"] != key]
    save(db, user_id, value)
    return {"capabilities": capabilities}


def limit(value, row, method, max_calls, seconds):
    now = time.time()
    calls = [t for t in row["rates"].get(method, []) if now - t < seconds]
    if len(calls) >= max_calls:
        external.fail(429, "activity_rate_limited")
    calls.append(now)
    row["rates"][method] = calls
    if method == "notification":
        recent = [stamp for stamp in value.get("notificationTimes", []) if now - stamp < 60]
        if len(recent) >= 10:
            external.fail(429, "notification_quota_exhausted")
        value["notificationTimes"] = recent + [now]
    return now


def badge(db, user_id, key, instance_id, count, module_id=None):
    runtime(db, user_id)
    db.expire_all()
    value, row, _ = authorize(db, user_id, key, instance_id, "badge", module_id)
    now = limit(value, row, "badge", 5, 10)
    if count:
        value["badges"][key] = {
            "count": count,
            "binding": row["binding"],
            "expiresAt": now + BADGE_TTL,
        }
    else:
        value["badges"].pop(key, None)
    save(db, user_id, value)
    return {"count": count}


def text(value):
    if not value.strip() or re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", value):
        external.fail(422, "invalid_notification_content")
    return value.strip()


def notify(db, user_id, key, instance_id, title, body, module_id=None):
    title, body = text(title), text(body)
    runtime(db, user_id)
    db.expire_all()
    value, row, _ = authorize(db, user_id, key, instance_id, "notification", module_id)
    now = limit(value, row, "notification", 3, 60)
    message = {
        "id": uuid4().hex,
        "applicationKey": key,
        "binding": row["binding"],
        "title": title,
        "body": body,
        "createdAt": now,
        "expiresAt": now + 300,
    }
    value["notifications"] = [n for n in value["notifications"] if n["expiresAt"] > now][-19:]
    value["notifications"].append(message)
    save(db, user_id, value)
    return {"id": message["id"]}


def projection(db, user_id):
    value = read(db, user_id)
    now = time.time()
    visible = {
        i["applicationKey"]: i
        for i in catalog.directory(db, user_id)["items"]
        if i["availability"]["available"]
    }

    def accepted(key, record, capability):
        item = visible.get(key)
        grant = value["grants"].get(key, {})
        return bool(
            item
            and record["binding"] == binding(item)
            and grant.get("binding") == record["binding"]
            and capability in grant.get("capabilities", [])
            and capability in item["declaration"].get("permissions", [])
        )

    badges = {
        k: {"count": v["count"]}
        for k, v in value["badges"].items()
        if v["expiresAt"] > now and accepted(k, v, "badge")
    }
    notices = [
        {k: n[k] for k in ["id", "applicationKey", "title", "body"]}
        for n in value["notifications"]
        if n["expiresAt"] > now and accepted(n["applicationKey"], n, "notification")
    ]
    background = [
        {
            "resourceId": rid,
            "applicationKey": v["applicationKey"],
            "instanceId": v["instanceId"],
            "expiresAt": v["expiresAt"],
        }
        for rid, v in value["background"].items()
        if v["expiresAt"] > now
        and v["deadline"] > now
        and accepted(v["applicationKey"], v, "background")
    ]
    return {"badges": badges, "notifications": notices, "background": background}


def dismiss(db, user_id, notification_id):
    runtime(db, user_id)
    db.expire_all()
    value = read(db, user_id)
    value["notifications"] = [n for n in value["notifications"] if n["id"] != notification_id]
    save(db, user_id, value)


def clear_instance(db, user_id, key, instance_id):
    runtime(db, user_id)
    db.expire_all()
    value = read(db, user_id)
    row = value["instances"].get(instance_id)
    if row and row["applicationKey"] != key:
        external.fail(404, "instance_not_found")
    value["instances"].pop(instance_id, None)
    save(db, user_id, value)


def revoke(db, user_id, application_key=None, install_id=None):
    """WB12 invalidation hook. Call background.stop_all before this metadata cleanup."""
    runtime(db, user_id)
    db.expire_all()
    value = read(db, user_id)

    def matches(key, record):
        return (application_key is None or key == application_key) and (
            install_id is None or record["binding"].get("installId") == install_id
        )

    for collection in ["grants", "badges"]:
        value[collection] = {k: v for k, v in value[collection].items() if not matches(k, v)}
    # Keep resource reservations until actual shutdown succeeds.
    for collection in ["instances"]:
        value[collection] = {
            k: v for k, v in value[collection].items() if not matches(v["applicationKey"], v)
        }
    value["notifications"] = [
        n for n in value["notifications"] if not matches(n["applicationKey"], n)
    ]
    save(db, user_id, value)
