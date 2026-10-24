"""Persist and publish uploaded plugin packages in the marketplace."""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

from core.harness.registry.marketplace_catalog import marketplace_catalog
from core.infra.time import utc_now
from core.capabilities.plugins.packaging.archive import extract_plugin_zip
from core.capabilities.plugins.packaging.importer import normalize_plugin_dir
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def market_row(db: Session, slug: str) -> Any:
    if not slug:
        return None
    return marketplace_catalog().market_package(db, slug)


def market_meta_dict(row: Any) -> Dict[str, Any]:
    """DB marketplace package → marketplace list metadata isomorphic to filesystem bundles."""
    return {
        "slug": row.slug,
        "name": row.name,
        "version": row.version or "1.0.0",
        "description": row.description or "",
        "category": row.category or "",
        "icon": row.icon,
        "skills_count": int(row.skills_count or 0),
        "required_secrets": list(row.required_secrets or []),
        "has_admin_config": bool(row.has_admin_config),
        "source": "uploaded",
    }


def publish_plugin_zip_to_market(
    db: Session, raw: bytes, *, created_by: Optional[str] = None
) -> Dict[str, Any]:
    """Admin uploads a plugin zip → published as a DB marketplace package (normalize validation + store the original zip); not installed.

    Once published it appears in the plugin marketplace list and can be
    explicitly installed by admins/users; re-uploading the same slug updates it.
    """
    with extract_plugin_zip(raw) as plugin_root:
        np = normalize_plugin_dir(
            plugin_root
        )  # parse/validate; invalid input raises immediately, nothing persisted
    package_b64 = base64.b64encode(raw).decode("ascii")
    has_admin_config = bool(np.admin_config and (np.admin_config.get("fields")))
    now = utc_now()
    fields = dict(
        name=np.name,
        version=np.version,
        description=np.description or "",
        category=np.category or "",
        icon=np.icon,
        kind=np.kind,
        skills_count=len(np.skills),
        required_secrets=list(np.required_secrets or []),
        has_admin_config=has_admin_config,
        package_b64=package_b64,
        updated_at=now,
    )
    action = marketplace_catalog().publish_package(db, np.slug, fields, created_by=created_by, now=now)
    logger.info(
        "plugin_market_%s: slug=%s kind=%s skills=%d", action, np.slug, np.kind, len(np.skills)
    )
    return {
        "slug": np.slug,
        "name": np.name,
        "kind": np.kind,
        "skills_count": len(np.skills),
        "action": action,
        "message": "插件已上架插件市场" if action == "published" else "插件市场内容已更新",
    }


def delete_market_package(db: Session, slug: str) -> Dict[str, Any]:
    """Remove an admin-uploaded DB marketplace package from the plugin marketplace (does not affect installed instances)."""
    marketplace_catalog().delete_package(db, slug)
    logger.info("plugin_market_deleted: slug=%s", slug)
    return {"slug": slug, "deleted": True}
