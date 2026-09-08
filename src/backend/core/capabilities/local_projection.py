"""Publish committed device DB skill mutations into the capability store/index."""

from __future__ import annotations

import threading
from . import registry, skills
from .paths import capabilities_enabled

_lock = threading.RLock()


def sync_local_skills() -> None:
    if not capabilities_enabled():
        return
    from core.db.models import AdminSkill
    from core.agent_skills.binary_files import decode_binary, is_binary_value
    from core.services.desktop_capability_protocol import skill_content_hash

    owners = set()
    with _lock:
        with registry._session() as db:
            from sqlalchemy import inspect

            if not inspect(db.get_bind()).has_table(AdminSkill.__tablename__):
                return
            rows = db.query(AdminSkill).order_by(AdminSkill.skill_id).all()
            definitions = [
                {
                    "skill_id": row.skill_id,
                    "content": row.skill_content,
                    "extra_files": dict(row.extra_files or {}),
                    "runtime_dependencies": dict(row.dependencies or {}),
                    "owner": row.owner_user_id,
                    "source_plugin": row.source_plugin,
                    "display_name": row.display_name,
                    "description": row.description,
                    "version": row.version,
                    "enabled": bool(row.is_enabled),
                }
                for row in rows
            ]
        live = {}
        for definition in definitions:
            sid, owner = definition["skill_id"], definition["owner"]
            existing = registry.get(registry.install_id("skill", "local", sid))
            if existing is not None and not existing.payload.get("from_db"):
                from .errors import NameConflict

                raise NameConflict(
                    "local upload key collides with an independent installation", runtime_name=sid
                )
            live[sid] = owner
            if owner:
                owners.add(owner)
            extras = definition["extra_files"]
            files = {
                "SKILL.md": definition["content"],
                **{
                    name: decode_binary(value) if is_binary_value(value) else str(value)
                    for name, value in extras.items()
                },
            }
            skills.publish_local_skill(
                sid,
                files=files,
                content_hash=skill_content_hash(definition["content"], extras),
                owner_user_id=owner,
                source="plugin" if definition["source_plugin"] else "local",
                source_plugin=definition["source_plugin"],
                display_name=definition["display_name"],
                description=definition["description"],
                version=definition["version"],
                enabled=definition["enabled"],
                from_db=True,
            )
            inst = registry.get(registry.install_id("skill", "local", sid))
            registry.set_state(
                inst.install_id,
                inst.state,
                payload_update={"runtime_dependencies": definition["runtime_dependencies"]},
            )
        for inst in registry.list_installations(kind="skill", profile_id="local"):
            if inst.payload.get("from_db") and inst.key not in live:
                if inst.payload.get("owner_user_id"):
                    owners.add(inst.payload["owner_user_id"])
        skills.prune_local(live)
    # Views depend on identity; do not hold the projection lock while resolving it.
    skills.rebuild_views(None)
    for owner in owners:
        skills.rebuild_user_view(owner)
