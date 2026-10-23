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
    from core.harness.registry.resource_owners import resource_owner
    return resource_owner().binding(db, user_id, key, install_id, revision, scope, module_id, selected)


def resolve_installation(db, row, user_id):
    if row.application_key is not None:
        from core.harness.registry.resource_owners import resource_owner
        return resource_owner().resolve_installation(db, row, user_id)
    from core.capabilities.plugins.resources.installation import resolve
    selected = resolve(db, row.slug, user_id, row.install_id)
    if selected.revision != row.revision:
        raise HTTPException(409, "plugin_revision_changed")
    return selected


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
