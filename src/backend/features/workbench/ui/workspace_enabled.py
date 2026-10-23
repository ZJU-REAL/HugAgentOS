"""Enablement for UI-only packages without altering legacy component semantics."""

import hashlib
from core.db.models import ContentBlock, UserShadow
from core.infra.time import utc_now
from sqlalchemy.orm.attributes import flag_modified


def workspace_only(row):
    components = row.component_ids or {}
    ui = row.ui_contributions or {}
    return (
        not components.get("skills")
        and not components.get("mcp")
        and ui.get("version") == 3
        and bool((ui.get("contributes") or {}).get("applications"))
    )


def preference_key(user_id, install_id):
    digest = hashlib.sha256((user_id + "\0" + install_id).encode()).hexdigest()
    return "workbench.plugin_enabled:" + digest


def enabled(db, row, user_id):
    if not workspace_only(row) or (row.import_report or {}).get("workspace_enabled") is False:
        return False
    preference = db.get(ContentBlock, preference_key(user_id, row.install_id)) if user_id else None
    return preference is None or preference.payload.get("enabled") is True


def set_enabled(db, row, value, user_id=None):
    if not workspace_only(row):
        return
    if user_id:
        if db.get(UserShadow, user_id) is None:
            raise ValueError("workspace_user_missing")
        # A dedicated row avoids read/modify/write races with unrelated user metadata.
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise ValueError("unsupported_workspace_database")
        payload = {"enabled": bool(value)}
        statement = insert(ContentBlock).values(
            id=preference_key(user_id, row.install_id), payload=payload, updated_at=utc_now()
        )
        db.execute(
            statement.on_conflict_do_update(
                index_elements=[ContentBlock.id], set_={"payload": payload, "updated_at": utc_now()}
            )
        )
        db.expire_all()
    else:
        row.import_report = {**(row.import_report or {}), "workspace_enabled": bool(value)}
        flag_modified(row, "import_report")
    db.commit()
