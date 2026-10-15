"""Resource owner validation and provider session identity."""

import hashlib
import json

from core.config.local_mode import local_mode_enabled
from fastapi import HTTPException


def execution_scope():
    return "local" if local_mode_enabled() else "cloud"


def runtime_session(row):
    if row.chat_id is not None:
        return row.chat_id
    # Provider session IDs are execution containers, never conversation records.
    binding = [
        row.user_id,
        row.application_key,
        row.install_id,
        row.revision,
        row.scope,
        row.instance_id,
    ]
    return "workbench-" + hashlib.sha256(json.dumps(binding).encode()).hexdigest()


def application_binding(db, user_id, key, install_id, revision, scope, module_id, selected=None):
    from core.applications import catalog

    item = catalog.readable(db, user_id, key)
    if not item["availability"]["available"]:
        raise HTTPException(403, "application_unavailable")
    binding = item.get("binding", {})
    if (
        item["declaration"]["target"]["kind"] != "plugin"
        or binding.get("installId") != install_id
        or (
            binding.get("packageRevision") != revision
            and (selected is None or selected.revision != revision)
        )
        or scope != execution_scope()
        or (binding.get("origin") is not None and binding["origin"].get("kind") != scope)
    ):
        raise HTTPException(409, "resource_binding_mismatch")
    target = item["declaration"]["target"]
    if selected is not None and binding.get("packageRevision") != revision:
        previous = next(
            (
                a
                for a in (selected.ui.get("contributes") or {}).get("applications", [])
                if a["id"] == item["declaration"]["id"]
            ),
            None,
        )
        if previous is None:
            raise HTTPException(403, "resource_application_removed")
        target = previous["target"]
    allowed = {target.get("moduleId")} | {
        view.get("moduleId") for view in item["declaration"].get("views", [])
    }
    if module_id not in allowed:
        raise HTTPException(403, "resource_module_not_granted")
    return item


def resolve_installation(db, row, user_id):
    from .installation import resolve

    if row.application_key is not None:
        from core.applications.installation_binding import exact as resolve

    selected = resolve(db, row.slug, user_id, row.install_id)
    if row.application_key is not None and selected.install_id != row.install_id:
        raise HTTPException(403, "resource_installation_mismatch")
    if selected.revision == row.revision:
        return selected
    if row.application_key is None:
        raise HTTPException(409, "plugin_revision_changed")
    try:
        from core.applications.plugin_revisions import resolve_revision
    except ModuleNotFoundError as error:
        if error.name != "core.applications.plugin_revisions":
            raise
        raise HTTPException(409, "revision_retention_unavailable") from None
    # WB13 rechecks current catalog permissions and the exact live instance lease.
    return resolve_revision(db, user_id, row.application_key, row.revision, row.instance_id)


def validate(db, row, user_id, selected=None):
    if row.chat_id is not None:
        if row.application_key is not None or row.instance_id is not None:
            raise HTTPException(403, "resource_owner_invalid")
        from core.db.models import ChatSession

        chat = db.get(ChatSession, row.chat_id)
        if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
            raise HTTPException(403, "conversation_unavailable")
    else:
        if not row.application_key or not row.instance_id:
            raise HTTPException(403, "resource_owner_invalid")
        application_binding(
            db,
            user_id,
            row.application_key,
            row.install_id,
            row.revision,
            row.scope,
            row.module_id,
            selected,
        )
