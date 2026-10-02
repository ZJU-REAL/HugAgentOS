"""Upgrade installed builtin Sites instructions without changing user connections."""

from __future__ import annotations

from core.infra.time import utc_now
from packaging.version import InvalidVersion, Version


def upgrade_builtin_sites(db) -> int:
    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
    from core.plugins.management import components as plugin_components
    from core.plugins.management import projection as plugin_projection
    from core.plugins.packaging import sources as plugin_sources
    from core.plugins.packaging.importer import normalize_plugin_dir

    bundle = plugin_sources._resolve_plugin_dir("sites")
    if bundle is None:
        return 0
    normalized = normalize_plugin_dir(bundle)
    target = Version(normalized.version)
    changed_owners = []
    installs = (
        db.query(InstalledPlugin)
        .filter(
            InstalledPlugin.slug == "sites",
            InstalledPlugin.source == "builtin",
        )
        .all()
    )
    for install in installs:
        try:
            if Version(install.version) >= target:
                continue
        except InvalidVersion:
            # Unknown/custom version semantics must not silently become a downgrade.
            continue
        owner = install.owner_user_id
        ids = install.component_ids or {}
        siblings = {
            sk.name: plugin_sources._make_skill_id("sites", sk.name, owner)
            for sk in normalized.skills
        }
        for sk in normalized.skills:
            sid = siblings[sk.name]
            row = db.get(AdminSkill, sid)
            if (
                sid not in ids.get("skills", [])
                or row is None
                or row.source_plugin != "sites"
                or row.owner_user_id != owner
            ):
                continue
            plugin_components._apply_skill(
                db,
                sk,
                slug="sites",
                owner_user_id=owner,
                secrets={},
                required_secrets=[],
                enabled=row.is_enabled,
                validate_ontology_build=False,
                sibling_ids=siblings,
            )
        for mc in normalized.mcp:
            sid = plugin_sources._make_server_id("sites", mc.name, owner)
            row = db.get(AdminMcpServer, sid)
            if (
                sid not in ids.get("mcp", [])
                or row is None
                or row.source_plugin != "sites"
                or row.owner_user_id != owner
            ):
                continue
            # Only shipped tool instructions change: URLs, credentials, runtime
            # settings and enabled states remain exactly as the user configured.
            row.tools_json = plugin_components._merge_tool_metadata(
                row.tools_json, list(mc.tools or [])
            )
            row.updated_at = utc_now()
        install.version = normalized.version
        install.updated_at = utc_now()
        _refresh_existing_projection(db, install)
        changed_owners.append(owner)
    if changed_owners:
        db.commit()
        for owner in set(changed_owners):
            plugin_projection._refresh_after_change(owner)
    return len(changed_owners)


def _refresh_existing_projection(db, install):
    """Update only the already-owned device snapshot, preserving enablement and edges."""
    from core.capabilities.paths import capabilities_enabled, revision_for_hash

    if not capabilities_enabled():
        return
    from core.capabilities import plugins, registry, store
    from core.services.desktop_capability_protocol import entity_content_hash

    current = registry.get("plugin:local:sites", db=db)
    if (
        not current
        or not current.ready
        or current.payload.get("db_install_id") != install.install_id
    ):
        return
    component = store.get("plugin", "local", "sites", current.resolved_revision)
    if component is None:
        return
    definition = plugins.load_manifest(component)
    definition["version"] = install.version
    files = plugins.plugin_manifest_files(definition)
    content_hash = entity_content_hash(files)
    revision = revision_for_hash(content_hash)
    if store.get("plugin", "local", "sites", revision) is None:
        store.write_from_files("plugin", "local", "sites", revision, files)
    registry.upsert(
        profile_id=current.profile_id,
        ref=current.ref,
        display_name=current.display_name,
        description=current.description,
        version=install.version,
        content_hash=content_hash,
        source=current.source,
        source_plugin=current.source_plugin,
        db=db,
    )
    registry.set_state(current.install_id, "ready", resolved_revision=revision, db=db)
