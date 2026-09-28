"""Upgrade installed official manager bundles; retain a rollback snapshot per owner."""
from pathlib import Path
from core.db.models import InstalledPlugin, AdminSkill, AdminMcpServer, ContentBlock
from core.services.management_contract import MANAGERS


def refresh(db):
    from core.capabilities.paths import capabilities_enabled
    if capabilities_enabled():
        return 0  # Device legacy packages are handled by local_legacy_plugin_migration.
    from core.services import plugin_service as ps
    root = Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace"
    count = 0
    for row in db.query(InstalledPlugin).filter(InstalledPlugin.slug.in_(list(MANAGERS))).all():
        if row.version == "2.0.0" or row.source != "builtin":
            continue
        servers = db.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == row.slug,
            AdminMcpServer.owner_user_id == row.owner_user_id).all()
        # Only platform-managed endpoints qualify; an external server with the same name does not.
        from core.config.settings import settings
        from mcp_servers._ports import PORTS
        name = row.slug.replace("-", "_")
        expected_url = f"http://{settings.server.mcp_host}:{PORTS[name]}/mcp/"
        if not servers or any(s.url != expected_url for s in servers):
            continue
        creator_skills = db.query(AdminSkill).filter(AdminSkill.source_plugin == row.slug,
            AdminSkill.owner_user_id == row.owner_user_id).all()
        backup_id = "manager-v2-backup:" + row.install_id
        if db.get(ContentBlock, backup_id) is None:
            db.add(ContentBlock(id=backup_id, payload={"version": row.version,
                "skills": [{"id": s.skill_id, "content": s.skill_content, "extra_files": s.extra_files,
                            "enabled": s.is_enabled} for s in creator_skills],
                "servers": [{"id": s.server_id, "tools": s.tools_json, "enabled": s.is_enabled} for s in servers]}))
            db.commit()
        enabled = {s.skill_id: s.is_enabled for s in creator_skills}
        enabled.update({s.server_id: s.is_enabled for s in servers})
        ps.import_plugin(db, root / row.slug, owner_user_id=row.owner_user_id, created_by="manager-v2-upgrade")
        # Official bundled schemas replace the retired verbs as one declaration.
        import json
        manifest = json.loads((root / row.slug / "plugin.json").read_text(encoding="utf-8"))
        declared_tools = manifest["extensions"]["org.hugagent"]["mcp"][name]["tools"]
        for server in servers:
            db.get(AdminMcpServer, server.server_id).tools_json = declared_tools
        for model, field in ((AdminSkill, "skill_id"), (AdminMcpServer, "server_id")):
            for item in db.query(model).filter(getattr(model, field).in_(list(enabled))).all():
                item.is_enabled = enabled[getattr(item, field)]
        db.commit()
        count += 1
    return count
