"""Protected operator snapshots. Never exposed by application HTTP routes."""

import json
import os
import stat
from hashlib import sha256
from pathlib import Path

from core.db.models import (
    AdminMcpServer,
    AdminSkill,
    CatalogOverride,
    ContentBlock,
    InstalledPlugin,
    UserShadow,
)
from sqlalchemy import inspect, or_

RECEIPT = "workbench_migration:v1"


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def state(db):
    """Capture migration scope, not passwords, files or unrelated business tables."""
    blocks = {
        row.id: row.payload
        for row in db.query(ContentBlock)
        .filter(
            or_(
                ContentBlock.id.in_(["app_config", "page_config"]),
                ContentBlock.id.startswith("workbench_", autoescape=True),
                ContentBlock.id.startswith("workbench.", autoescape=True),
            ),
            ContentBlock.id.notin_([RECEIPT, "workbench_namespace_lock"]),
            ~ContentBlock.id.startswith("workbench_lock:", autoescape=True),
        )
        .order_by(ContentBlock.id)
    }
    users = {
        row.user_id: row.extra_data for row in db.query(UserShadow).order_by(UserShadow.user_id)
    }
    plugins = {
        row.install_id: {
            "owner": row.owner_user_id,
            "slug": row.slug,
            "ui": row.ui_contributions,
            "version": row.version,
            "source": row.source,
            "components": row.component_ids,
            "importReport": row.import_report,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row in db.query(InstalledPlugin).order_by(InstalledPlugin.install_id)
    }
    from features.workbench.edition import snapshot_permissions

    permissions = snapshot_permissions(db)
    permissions["pluginSkills"] = {
        row.skill_id: {
            "enabled": row.is_enabled,
            "owner": row.owner_user_id,
            "source": row.source_plugin,
            "ready": row.dep_status,
        }
        for row in db.query(AdminSkill)
        .filter(AdminSkill.source_plugin.isnot(None))
        .order_by(AdminSkill.skill_id)
    }
    permissions["pluginMcps"] = {
        row.server_id: {
            "enabled": row.is_enabled,
            "owner": row.owner_user_id,
            "source": row.source_plugin,
        }
        for row in db.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin.isnot(None))
        .order_by(AdminMcpServer.server_id)
    }
    permissions["overrides"] = [
        {
            "user": row.user_id,
            "kind": row.kind,
            "item": row.item_id,
            "enabled": row.enabled,
            "config": row.config_data,
        }
        for row in db.query(CatalogOverride).order_by(
            CatalogOverride.user_id, CatalogOverride.kind, CatalogOverride.item_id
        )
    ]
    return {"blocks": blocks, "users": users, "plugins": plugins, "permissions": permissions}


def write_snapshot(path, value):
    """Exclusive create, owner-only file and directory; fail before any DB mutation."""
    path = Path(path)
    if not path.is_absolute():
        path = path.absolute()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or any(p.is_symlink() for p in parent.parents):
        raise ValueError("unsafe_snapshot_path")
    if parent.stat().st_uid != os.getuid() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ValueError("snapshot_directory_not_private")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return digest(value)


def read_snapshot(path):
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        status = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) & 0o077
        ):
            raise ValueError("snapshot_not_private")
        return json.load(stream)
