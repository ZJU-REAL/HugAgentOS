"""Plugin installation responsibilities."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db.models import AdminMcpServer, AdminSkill, ContentBlock, InstalledPlugin
from core.infra.exceptions import ResourceNotFoundError
from core.infra.time import utc_now
from core.plugins.management import components as plugin_components
from core.plugins.management import details as plugin_details
from core.plugins.management import market as plugin_market
from core.plugins.management import packages as plugin_packages
from core.plugins.management import projection as plugin_projection
from core.plugins.packaging import definitions as plugin_definitions
from core.plugins.packaging import sources as plugin_sources
from core.plugins.packaging.archive import _extract_plugin_zip
from core.plugins.packaging.importer import normalize_plugin_dir
from core.plugins.packaging.models import NormalizedPlugin
from core.services.ontology_policy import resolve_plugin_import_ontology_validation
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


def _apply_normalized(
    db: Session,
    np: NormalizedPlugin,
    *,
    owner_user_id: Optional[str],
    secrets: Dict[str, str],
    source: str,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a NormalizedPlugin and return the installation result (including the import_report)."""
    install_id = plugin_sources._make_plugin_install_id(np.slug, owner_user_id)
    existing = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    previous_component_ids = dict(existing.component_ids or {}) if existing is not None else {}
    ontology_policy = resolve_plugin_import_ontology_validation(db, owner_user_id)

    de_skills = set(np.default_enabled.get("skills") or [])
    de_mcp = set(np.default_enabled.get("mcp") or [])

    # Skills the admin removed from the marketplace: excluded at install time (applies uniformly to builtin/uploaded packages)
    excluded = plugin_details.get_market_skill_exclusions(db, np.slug)
    install_skills = [sk for sk in np.skills if sk.name not in excluded]

    imported: List[Dict[str, str]] = []
    adapted: List[Dict[str, str]] = []
    skill_ids: List[str] = []
    server_ids: List[str] = []

    # Sibling skill name → final skill_id mapping (used to rewrite inter-skill ../<name> relative references)
    sibling_ids = {
        sk.name: plugin_sources._make_skill_id(np.slug, sk.name, owner_user_id)
        for sk in install_skills
    }

    for sk in install_skills:
        sid = plugin_components._apply_skill(
            db,
            sk,
            slug=np.slug,
            owner_user_id=owner_user_id,
            secrets=secrets,
            required_secrets=np.required_secrets,
            enabled=(sk.name in de_skills) or not de_skills,
            validate_ontology_build=ontology_policy.enabled,
            sibling_ids=sibling_ids,
        )
        skill_ids.append(sid)
        imported.append({"type": "skill", "id": sid, "name": sk.name})

    for mc in np.mcp:
        sid = plugin_components._apply_mcp(
            db,
            mc,
            slug=np.slug,
            owner_user_id=owner_user_id,
            enabled=(mc.name in de_mcp),
            validate_ontology_build=ontology_policy.enabled,
        )
        server_ids.append(sid)
        if mc.needs_runtime:
            adapted.append(
                {
                    "type": "mcp",
                    "id": sid,
                    "name": mc.name,
                    "note": mc.note or "stdio MCP 已装上但禁用，需运行时",
                }
            )
        else:
            imported.append({"type": "mcp", "id": sid, "name": mc.name})

    import_report = {"imported": imported, "adapted": adapted, "dropped": np.dropped}
    if np.package_dir and np.ui:
        from core.plugins.packaging.runtime_assets import retain
        import_report["package_revision"] = retain(db, Path(np.package_dir))
    component_ids = {"skills": skill_ids, "mcp": server_ids, "prompts": []}

    # A plugin update is a replacement of its declared component set. Remove
    # components that belonged to the previous version but no longer appear in
    # the new manifest; otherwise retired tools/skills remain visible forever.
    stale_skill_ids = set(plugin_sources._component_keys(previous_component_ids, "skills")) - set(
        skill_ids
    )
    stale_server_ids = set(plugin_sources._component_keys(previous_component_ids, "mcp")) - set(
        server_ids
    )
    owner_skill_filter = (
        AdminSkill.owner_user_id == owner_user_id
        if owner_user_id is not None
        else AdminSkill.owner_user_id.is_(None)
    )
    owner_mcp_filter = (
        AdminMcpServer.owner_user_id == owner_user_id
        if owner_user_id is not None
        else AdminMcpServer.owner_user_id.is_(None)
    )
    if stale_skill_ids:
        (
            db.query(AdminSkill)
            .filter(
                AdminSkill.skill_id.in_(stale_skill_ids),
                AdminSkill.source_plugin == np.slug,
                owner_skill_filter,
            )
            .delete(synchronize_session=False)
        )
        plugin_projection._purge_sandbox_skill_files(stale_skill_ids)
    if stale_server_ids:
        (
            db.query(AdminMcpServer)
            .filter(
                AdminMcpServer.server_id.in_(stale_server_ids),
                AdminMcpServer.source_plugin == np.slug,
                owner_mcp_filter,
            )
            .delete(synchronize_session=False)
        )

    now = utc_now()
    # Display metadata for the installed record: UI-configured market metadata
    # (DB override → builtin seed) wins over whatever the manifest carried.
    market_meta = plugin_market.resolve_market_meta(db, np.slug)
    fields = dict(
        slug=np.slug,
        name=market_meta.get("display_name") or np.name,
        version=np.version,
        description=np.description,
        category=market_meta.get("category") or np.category,
        icon=market_meta.get("icon") or np.icon,
        owner_user_id=owner_user_id,
        source=source,
        component_ids=component_ids,
        import_report=import_report,
        # Reinstall/upgrade re-reads the manifest, so a plugin that dropped its
        # ``ui`` block correctly ends up with NULL here and loses its interface.
        ui_contributions=np.ui,
        updated_at=now,
    )
    if existing is not None:
        for key, val in fields.items():
            setattr(existing, key, val)
        for col in ("component_ids", "import_report", "ui_contributions"):
            flag_modified(existing, col)
        action = "updated"
    else:
        db.add(
            InstalledPlugin(install_id=install_id, created_at=now, created_by=created_by, **fields)
        )
        action = "installed"

    db.commit()
    plugin_projection._refresh_after_change(owner_user_id)
    plugin_projection._project_plugin_to_store(
        {
            "install_id": install_id,
            "slug": np.slug,
            "name": fields["name"],
            "version": np.version,
            "description": np.description,
            "category": fields["category"],
            "icon": fields["icon"],
            "components": component_ids,
            "ui_contributions": np.ui,
            "import_report": import_report,
        },
        owner_user_id=owner_user_id,
    )
    # 插件在本机需要的资产（如站点插件的建站模板）跟着插件走：装到哪台机器上，就在
    # 哪台机器上铺。云端同步装的那条走 desktop_cloud_bundles 的发布钩子，同一张表。
    from core.plugins.local.assets import provision_for

    provision_for(np.slug)
    logger.info(
        "plugin_%s: slug=%s kind=%s owner=%s skills=%d mcp=%d dropped=%d "
        "ontology_validation=%s forced=%s",
        action,
        np.slug,
        np.kind,
        owner_user_id or "global",
        len(skill_ids),
        len(server_ids),
        len(np.dropped),
        ontology_policy.enabled,
        ontology_policy.forced,
    )
    return {
        "install_id": install_id,
        "slug": np.slug,
        "name": np.name,
        "kind": np.kind,
        "action": action,
        "import_report": import_report,
    }


def install_plugin(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Install a marketplace plugin package (filesystem default / marketplace, or a DB-published marketplace package)."""
    plugin_dir = plugin_sources._resolve_plugin_dir(slug)
    if plugin_dir is not None:
        np = normalize_plugin_dir(plugin_dir)
        return _apply_normalized(
            db,
            np,
            owner_user_id=owner_user_id,
            secrets=secrets or {},
            source="builtin",
            created_by=created_by,
        )
    # DB-published marketplace package: extract the original zip and go through the same path as import
    row = plugin_packages._market_row(db, slug)
    if row is not None:
        with _extract_plugin_zip(base64.b64decode(row.package_b64)) as plugin_root:
            return import_plugin(
                db,
                plugin_root,
                owner_user_id=owner_user_id,
                secrets=secrets or {},
                created_by=created_by,
            )
    raise ResourceNotFoundError("plugin", slug)


def ensure_default_plugins_bootstrapped(db: Session) -> bool:
    """Install the CE default plugin set exactly once for a persistent DB.

    The marker is deliberately independent from the current installation rows:
    after the first successful bootstrap, uninstalling a default plugin is a
    user choice and must survive backend/container restarts. A partial failure
    writes no marker, so the next startup idempotently retries the complete set.

    Returns ``True`` only when this call completes the first bootstrap.
    """
    marker = (
        db.query(ContentBlock)
        .filter(ContentBlock.id == plugin_definitions.DEFAULT_BOOTSTRAP_MARKER_ID)
        .first()
    )
    if marker is not None:
        return False

    installed: List[str] = []
    try:
        for slug in plugin_definitions.DEFAULT_BOOTSTRAP_PLUGIN_SLUGS:
            install_plugin(
                db,
                slug,
                owner_user_id=None,
                created_by="system_bootstrap",
            )
            installed.append(slug)
    except Exception:
        db.rollback()
        raise

    db.add(
        ContentBlock(
            id=plugin_definitions.DEFAULT_BOOTSTRAP_MARKER_ID,
            payload={"version": 1, "plugins": installed},
            updated_by="system_bootstrap",
        )
    )
    db.commit()
    return True


def import_plugin(
    db: Session,
    plugin_dir: Path,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Import an external plugin directory (native / Claude Code / Codex), persist it, and return the import_report."""
    np = normalize_plugin_dir(plugin_dir)
    source = {"claude": "imported_claude", "codex": "imported_codex"}.get(np.kind, "builtin")
    return _apply_normalized(
        db,
        np,
        owner_user_id=owner_user_id,
        secrets=secrets or {},
        source=source,
        created_by=created_by,
    )


def import_plugin_from_zip(
    db: Session,
    raw: bytes,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Import a plugin from uploaded zip bytes (extract → locate root → import_plugin). Shared by user and admin uploads."""
    with _extract_plugin_zip(raw) as plugin_root:
        return import_plugin(
            db,
            plugin_root,
            owner_user_id=owner_user_id,
            secrets=secrets,
            created_by=created_by,
        )
