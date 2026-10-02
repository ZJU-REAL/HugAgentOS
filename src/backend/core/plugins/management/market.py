"""Plugin market responsibilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.models import (
    ContentBlock,
    InstalledPlugin,
    PluginMarketPackage,
    PluginMarketSkillExclusion,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.plugins.management import packages as plugin_packages
from core.plugins.packaging import definitions as plugin_definitions
from core.plugins.packaging import sources as plugin_sources
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


def _validate_icon(icon: str) -> str:
    """Validate an icon value from the UI picker; returns the stripped value ('' = clear)."""
    icon = (icon or "").strip()
    if not icon:
        return ""
    if icon.startswith("data:"):
        if not icon.startswith("data:image/"):
            raise BadRequestError(message="图标 data URI 必须是 image 类型")
        if len(icon) > plugin_definitions.MAX_ICON_LEN:
            raise BadRequestError(message="图标过大（上传原图请控制在 80KB 以内）")
        return icon
    if not (icon.startswith("/") or icon.startswith("http://") or icon.startswith("https://")):
        raise BadRequestError(message="图标须从图标库选择或上传（不支持任意文本）")
    if len(icon) > plugin_definitions.MAX_ICON_URL_LEN:
        raise BadRequestError(message="图标地址过长")
    return icon


def _market_meta_overrides(db: Optional[Session]) -> Dict[str, Dict[str, Any]]:
    """Admin display-metadata overrides for market plugins ({slug: {display_name, category, icon}})."""
    if db is None:
        return {}
    row = (
        db.query(ContentBlock)
        .filter(ContentBlock.id == plugin_definitions.PLUGIN_MARKET_META_BLOCK_ID)
        .first()
    )
    payload = row.payload if row is not None and isinstance(row.payload, dict) else {}
    return {k: v for k, v in payload.items() if isinstance(v, dict)}


def resolve_market_meta(db: Optional[Session], slug: str) -> Dict[str, Any]:
    """Effective display metadata for one market plugin: DB override → builtin seed."""
    meta = dict(plugin_definitions.BUILTIN_PLUGIN_MARKET_META.get(slug) or {})
    override = _market_meta_overrides(db).get(slug) or {}
    for k in plugin_definitions._META_KEYS:
        v = override.get(k)
        if isinstance(v, str) and v.strip():
            meta[k] = v.strip()
    return meta


def _overlay_market_meta(item: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """Apply effective display metadata onto a market list/detail dict (in place)."""
    if meta.get("display_name"):
        item["name"] = meta["display_name"]
    if meta.get("category"):
        item["category"] = meta["category"]
    if meta.get("icon"):
        item["icon"] = meta["icon"]


def set_market_meta(
    db: Session,
    slug: str,
    *,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
    icon: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Admin: set a market plugin's display metadata (display_name/category/icon).

    Only provided fields are written; passing an empty string clears the
    override (falls back to the builtin seed). The slug must exist in the
    market (filesystem bundle or uploaded DB package).
    """
    if (
        plugin_sources._resolve_plugin_dir(slug) is None
        and plugin_packages._market_row(db, slug) is None
    ):
        raise ResourceNotFoundError("plugin", slug)
    row = (
        db.query(ContentBlock)
        .filter(ContentBlock.id == plugin_definitions.PLUGIN_MARKET_META_BLOCK_ID)
        .first()
    )
    payload = dict(row.payload or {}) if row is not None else {}
    entry = dict(payload.get(slug) or {})
    if icon is not None:
        icon = _validate_icon(icon)
    for key, val in (("display_name", display_name), ("category", category), ("icon", icon)):
        if val is None:
            continue
        val = val.strip()
        if val:
            entry[key] = val
        else:
            entry.pop(key, None)
    if entry:
        payload[slug] = entry
    else:
        payload.pop(slug, None)
    if row is not None:
        row.payload = payload
        flag_modified(row, "payload")
        row.updated_by = updated_by or "admin"
    else:
        db.add(
            ContentBlock(
                id=plugin_definitions.PLUGIN_MARKET_META_BLOCK_ID,
                payload=payload,
                updated_by=updated_by or "admin",
            )
        )
    db.commit()
    logger.info("plugin_market_meta_set: slug=%s keys=%s", slug, sorted(entry.keys()))
    return {"slug": slug, **resolve_market_meta(db, slug)}


def list_plugins(
    db: Session, owner_user_id: Optional[str], *, include_disabled: bool = False
) -> List[Dict[str, Any]]:
    """Plugin marketplace list (scans plugin_bundles/{default,marketplace}, annotated with installed status).

    ``include_disabled``: the admin panel passes True (sees everything +
    ``market_enabled`` annotation); the user side defaults to False (sees only
    published entries).
    """
    items: List[Dict[str, Any]] = []
    seen_slugs: set = set()
    for child in plugin_sources._iter_plugin_dirs():
        meta = plugin_sources._scan_native_manifest(child)
        if meta:
            meta["source"] = "builtin"
            items.append(meta)
            seen_slugs.add(meta["slug"])
    # DB marketplace packages (admin-uploaded and published): skipped when the filesystem already has the same slug
    for row in db.query(PluginMarketPackage).order_by(PluginMarketPackage.created_at.desc()).all():
        if row.slug in seen_slugs:
            continue
        items.append(plugin_packages._market_meta_dict(row))
        seen_slugs.add(row.slug)
    # Display metadata is UI configuration: DB override → builtin seed → manifest fallback
    overrides = _market_meta_overrides(db)
    for it in items:
        meta = dict(plugin_definitions.BUILTIN_PLUGIN_MARKET_META.get(it["slug"]) or {})
        for k in plugin_definitions._META_KEYS:
            v = (overrides.get(it["slug"]) or {}).get(k)
            if isinstance(v, str) and v.strip():
                meta[k] = v.strip()
        _overlay_market_meta(it, meta)
    # Subtract skills the admin removed from the marketplace (aggregated once per slug) so skills_count reflects the real offering
    excl_by_slug: Dict[str, set] = {}
    for ex_slug, ex_name in db.query(
        PluginMarketSkillExclusion.slug, PluginMarketSkillExclusion.skill_name
    ).all():
        excl_by_slug.setdefault(ex_slug, set()).add(ex_name)
    if excl_by_slug:
        for it in items:
            n_excl = len(excl_by_slug.get(it["slug"], ()))
            if n_excl:
                it["skills_count"] = max(0, int(it.get("skills_count") or 0) - n_excl)

    # Annotate installed status
    if items:
        install_ids = {
            it["slug"]: plugin_sources._make_plugin_install_id(it["slug"], owner_user_id)
            for it in items
        }
        present = {
            row[0]
            for row in db.query(InstalledPlugin.install_id)
            .filter(InstalledPlugin.install_id.in_(list(install_ids.values())))
            .all()
        }
        for it in items:
            it["installed"] = install_ids[it["slug"]] in present

    # Marketplace publish toggle + visibility scope: users see only published
    # entries visible to them; the admin panel sees everything with
    # annotations. For user-side callers owner_user_id is the current browsing
    # user, reused directly as the viewer.
    from core.services import marketplace_listing as ml

    items = ml.annotate_and_filter(
        db,
        ml.KIND_PLUGIN,
        items,
        id_key="slug",
        include_disabled=include_disabled,
        viewer_user_id=owner_user_id,
    )
    return items


def set_plugin_market_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """Publish/unpublish a marketplace plugin (controls display in the plugin marketplace; does not affect installed instances)."""
    from core.services import marketplace_listing as ml

    res = ml.set_listing_enabled(db, ml.KIND_PLUGIN, slug, enabled, updated_by=updated_by)
    logger.info("plugin_market_listing: slug=%s enabled=%s", slug, enabled)
    return res
