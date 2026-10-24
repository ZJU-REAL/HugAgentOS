"""Private validators derived from the current authorized projection, not a second truth."""

import hashlib
import json

from features.workbench import catalog
from core.config.local_mode import local_mode_enabled
from core.db.models import ContentBlock


def snapshot(db, user_id):
    projection = catalog.directory(db, user_id)
    scope = "local" if local_mode_enabled() else "cloud"
    visible = {item["applicationKey"] for item in projection["items"]}
    auth_revisions = sorted(
        (row.id, row.payload.get("revision", 0))
        for row in db.query(ContentBlock)
        .filter(ContentBlock.id.startswith("workbench_external_auth:", autoescape=True))
        .all()
        if row.id.removeprefix("workbench_external_auth:") in visible
    )
    encoded = json.dumps(
        [user_id, scope, projection, auth_revisions],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    revision = hashlib.sha256(encoded).hexdigest()
    return {**projection, "revision": revision}, scope


def matches(header, revision):
    return (header or "").strip() == "*" or any(
        value.strip().removeprefix("W/") == '"' + revision + '"'
        for value in (header or "").split(",")
    )
