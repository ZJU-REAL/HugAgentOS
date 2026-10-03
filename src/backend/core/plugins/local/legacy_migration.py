"""One-way legacy plugin migration with encrypted rollback snapshots and stable child IDs."""

import hashlib
import json
import tempfile
from pathlib import Path

from core.agent_skills.binary_files import decode_binary, is_binary_value
from core.capabilities import archive, registry
from core.db.models import AdminMcpServer, AdminSkill, ContentBlock, InstalledPlugin
from core.plugins.local import service as lifecycle
from core.plugins.packaging import sources as plugin_sources
from core.services.management_package import serialized


def _backup(db, row, children, servers):
    from core.infra.crypto import encrypt_secret

    def values(obj):
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

    key = "local-plugin-v1:" + hashlib.sha256(row.install_id.encode()).hexdigest()
    if db.get(ContentBlock, key) is None:
        data = {
            "plugin": values(row),
            "skills": [values(x) for x in children],
            "mcp": [values(x) for x in servers],
        }
        db.add(
            ContentBlock(
                id=key,
                payload={"encrypted": encrypt_secret(json.dumps(data, default=str)), "version": 1},
            )
        )


def _package(root, row, children, servers):
    from core.services.management_contract import MANAGERS

    skill_ids, server_ids = {}, {}
    official = row.source == "builtin" and row.slug in MANAGERS
    if official:
        from core.config.settings import settings
        from mcp_servers._ports import PORTS

        expected = f"http://{settings.server.mcp_host}:{PORTS[row.slug.replace('-', '_')]}/mcp/"
        official = bool(servers) and all(s.url == expected for s in servers)
    if official:
        package = Path(__file__).resolve().parents[3] / "plugin_bundles/marketplace" / row.slug
        archive.write_files(root, {n: p.read_bytes() for n, p in archive.iter_files(package)})
        from core.plugins.packaging.importer import normalize_plugin_dir

        np = normalize_plugin_dir(root)
        skill_ids = {
            s.name: plugin_sources._make_skill_id(row.slug, s.name, row.owner_user_id)
            for s in np.skills
        }
        server_ids = {
            s.name: plugin_sources._make_server_id(row.slug, s.name, row.owner_user_id)
            for s in np.mcp
        }
        return skill_ids, server_ids
    for child in children:
        skill_ids[child.skill_id] = child.skill_id
        files = {
            "SKILL.md": child.skill_content,
            **{
                n: decode_binary(v) if is_binary_value(v) else v
                for n, v in (child.extra_files or {}).items()
            },
        }
        archive.write_files(root / "skills" / child.skill_id, files)
    mcp = {}
    for server in servers:
        # Credentials already belong to the local installation. Do not copy them
        # into immutable package bytes; retain the encrypted legacy backup first.
        if server.headers:
            raise ValueError(
                "legacy plugin with credentials requires explicit secure reconfiguration"
            )
        server_ids[server.server_id] = server.server_id
        mcp[server.server_id] = {
            "transport": server.transport,
            "url": server.url,
            "command": server.command,
            "args": server.args or [],
            "env": server.env_vars or {},
            "tools": server.tools_json or [],
        }
    definition = {
        "name": row.slug,
        "version": row.version or "1",
        "description": row.description or row.name,
        "mcpServers": mcp,
        "default_enabled": {
            "skills": [x.skill_id for x in children if x.is_enabled],
            "mcp": [x.server_id for x in servers if x.is_enabled],
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(definition), encoding="utf-8")
    return skill_ids, server_ids


@serialized
def migrate():
    from core.capabilities import device_catalog
    from core.capabilities.paths import capabilities_enabled
    from sqlalchemy import inspect

    if not capabilities_enabled():
        return 0
    count = 0
    with registry._session() as db:
        if not inspect(db.get_bind()).has_table(InstalledPlugin.__tablename__):
            return 0
        for row in db.query(InstalledPlugin).all():
            # Hybrid shared defaults are replaced by the authenticated cloud manifest.
            if row.owner_user_id is None and device_catalog.active():
                continue
            children = (
                db.query(AdminSkill)
                .filter(
                    AdminSkill.source_plugin == row.slug,
                    AdminSkill.owner_user_id == row.owner_user_id,
                )
                .all()
            )
            servers = (
                db.query(AdminMcpServer)
                .filter(
                    AdminMcpServer.source_plugin == row.slug,
                    AdminMcpServer.owner_user_id == row.owner_user_id,
                )
                .all()
            )
            _backup(db, row, children, servers)
            with tempfile.TemporaryDirectory(prefix="legacy-plugin-") as tmp:
                root = Path(tmp) / "plugin"
                skill_ids, server_ids = _package(root, row, children, servers)
                prepared = lifecycle._prepare(
                    row.owner_user_id, root, skill_ids=skill_ids, server_ids=server_ids
                )
            iid = registry.install_id("plugin", "local", prepared[1])
            current = registry.get(iid, db=db)
            if not current:
                iid = lifecycle._publish(row.owner_user_id, prepared, db, legacy_source=row.slug)
                registry.set_state(
                    iid,
                    "ready",
                    payload_update={
                        "shared_installation": row.owner_user_id is None,
                        "legacy_ids": [row.install_id, row.slug],
                        "migrated_from": "installed_plugins",
                        "presentation": {
                            "display_name": row.name,
                            "icon": row.icon,
                            "category": row.category,
                        },
                    },
                    db=db,
                )
                for child in children:
                    registry.set_enabled(
                        registry.install_id("skill", "local", child.skill_id),
                        child.is_enabled,
                        db=db,
                    )
            elif current.payload.get("owner_user_id") != row.owner_user_id:
                raise ValueError("legacy plugin ownership conflict")
            old_id = registry.install_id("plugin", "local", row.slug)
            if (
                old_id != iid
                and (old := registry.get(old_id, db=db))
                and old.payload.get("from_db")
                and old.payload.get("owner_user_id") == row.owner_user_id
            ):
                registry.mark_removed(old_id, db=db)
            for item in [*children, *servers, row]:
                db.delete(item)
            count += 1
    return count
