"""Move legacy desktop skill rows once into the authoritative capability index.

Backups and row removal share the registry transaction. Historical immutable bytes
remain available; a removed installation is never reactivated by a legacy row.
Plugin-owned rows remain owned by their legacy plugin until that package migrates.
"""
import hashlib
import tempfile
from pathlib import Path
from sqlalchemy import inspect
from core.capabilities import archive, registry
from core.capabilities.paths import capabilities_enabled
from core.capabilities.ref import local_ref
from core.db.models import AdminSkill, ContentBlock
from core.services.management_package import serialized
from core.services import local_skill_service as lifecycle
from core.agent_skills.binary_files import decode_binary, is_binary_value


def _backup(db, row):
    key = "local-management-v1:" + hashlib.sha256(row.skill_id.encode()).hexdigest()
    if db.get(ContentBlock, key) is None:
        payload = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        # Datetimes must round-trip through the JSON backup without custom encoders.
        payload = {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in payload.items()}
        import json
        from core.infra.crypto import encrypt_secret
        db.add(ContentBlock(id=key, payload={"encrypted": encrypt_secret(json.dumps({"table": "admin_skills", "row": payload})), "version": 1}))


@serialized
def migrate():
    if not capabilities_enabled():
        return 0
    count = 0
    with registry._session() as db:
        if not inspect(db.get_bind()).has_table(AdminSkill.__tablename__):
            return 0
        rows = db.query(AdminSkill).filter(AdminSkill.source_plugin.is_(None)).all()
        for row in rows:
            # Shared administrator content stays on its existing admin lifecycle.
            # This migration concerns user-owned installations managed by these plugins.
            if not row.owner_user_id:
                continue
            iid = registry.install_id("skill", "local", row.skill_id)
            existing = registry.get(iid, db=db)
            if existing and not existing.payload.get("from_db"):
                if existing.payload.get("owner_user_id") != row.owner_user_id:
                    raise ValueError("legacy skill ownership conflict")
                _backup(db, row)
                db.delete(row)
                count += 1
                continue
            files = {"SKILL.md": row.skill_content, **{n: decode_binary(v) if is_binary_value(v) else v
                                                     for n, v in (row.extra_files or {}).items()}}
            with tempfile.TemporaryDirectory(prefix="legacy-skill-") as tmp:
                root = Path(tmp) / "skill"
                archive.write_files(root, files)
                key, meta, digest, comp, _ = lifecycle._prepare(row.owner_user_id, root, row.skill_id)
            _backup(db, row)
            if not existing or existing.state != "removed":
                registry.upsert(profile_id="local", ref=local_ref("skill", key),
                    display_name=row.display_name, description=meta.description, version=meta.version,
                    content_hash=digest, source="local", enabled=row.is_enabled,
                    payload={"owner_user_id": row.owner_user_id, "from_db": False,
                             "managed_by": "skill-manager", "migrated_from": "admin_skills",
                             "presentation": {"user_intro": row.user_intro}}, db=db)
                registry.set_state(iid, "ready", resolved_revision=comp.revision, db=db)
            db.delete(row)
            count += 1
    return count
