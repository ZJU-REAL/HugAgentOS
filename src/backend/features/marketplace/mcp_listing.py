"""mcp catalog responsibilities."""
from __future__ import annotations
from typing import Any, Dict, Optional
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketVersion
from core.infra.exceptions import ResourceNotFoundError
from features.marketplace import edition as ml
from sqlalchemy.orm import Session
from features.marketplace import mcp_credentials
from features.marketplace import mcp_oauth_clients
from features.marketplace import mcp_state
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

def installed_slug_set(db: Session, owner_user_id: Optional[str]) -> set[str]:
    query = db.query(McpMarketInstallation.slug).filter(
        McpMarketInstallation.status.in_(["active", "suspended"])
    )
    query = (
        query.filter(McpMarketInstallation.owner_user_id.is_(None))
        if owner_user_id is None
        else query.filter(McpMarketInstallation.owner_user_id == owner_user_id)
    )
    return {str(row[0]) for row in query.all()}


def item_dict(
    db: Session,
    item: McpMarketItem,
    version: McpMarketVersion,
    *,
    installed: bool = False,
) -> Dict[str, Any]:
    auth_schema = list(version.auth_schema or [])
    auth_config = mcp_validation.normalize_auth_config(version.auth_config, auth_schema)
    requires_auth = any(method["type"] != "none" for method in auth_config["methods"])
    credentials_managed_by_admin = bool(
        mcp_credentials.admin_managed_credential_headers(
            db,
            item,
            version,
            auth_method=str(auth_config["default_method"]),
        )
    )
    if auth_config["credential_mode"] == "auto":
        auth_config["credential_mode"] = "admin" if credentials_managed_by_admin else "installer"
    auth_config = mcp_oauth_clients.present_oauth_methods(db, item.slug, auth_config)
    supports_admin_credentials = bool(mcp_credentials.admin_credential_source(db, item, version))
    return {
        "slug": item.slug,
        "display_name": item.display_name,
        "description": item.description or "",
        "summary": item.description or "",
        "user_intro": item.user_intro or "",
        "category": item.category,
        "tags": list(item.tags or []),
        "icon": item.icon or "",
        "publisher_name": item.publisher_name or "",
        "source": item.source,
        "version": version.version,
        "version_id": version.version_id,
        "transport": version.transport,
        "url_origin": mcp_validation.safe_origin(version.url),
        "auth_schema": auth_schema,
        "auth_config": auth_config,
        "requires_auth": requires_auth,
        "credentials_managed_by_admin": credentials_managed_by_admin,
        "supports_admin_credentials": supports_admin_credentials,
        # Curated templates are re-seeded from code; only their shared OAuth App is editable.
        "curated": version.approved_by == "system-curated",
        "requires_user_credentials": requires_auth and not credentials_managed_by_admin,
        "tools": list(version.tools_json or []),
        "tool_count": len(version.tools_json or []),
        "tool_hash": version.tool_hash,
        "listing_notice": dict(version.listing_notice or {}),
        "status": item.status,
        "status_reason": item.status_reason or "",
        "last_verified_at": item.last_verified_at.isoformat() if item.last_verified_at else None,
        "installed": installed,
        "deletable": True,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def list_market_items(
    db: Session,
    *,
    owner_user_id: Optional[str],
    viewer_user_id: Optional[str],
    include_disabled: bool = False,
) -> Dict[str, Any]:
    query = db.query(McpMarketItem).filter(McpMarketItem.deleted_at.is_(None))
    if not include_disabled:
        query = query.filter(McpMarketItem.status == "active")
    rows = query.order_by(McpMarketItem.updated_at.desc(), McpMarketItem.slug).all()
    installed = installed_slug_set(db, owner_user_id)
    items = [
        item_dict(db, row, mcp_versions.version_for_item(db, row), installed=row.slug in installed)
        for row in rows
    ]
    items = ml.annotate_and_filter(
        db,
        ml.KIND_MCP,
        items,
        id_key="slug",
        include_disabled=include_disabled,
        viewer_user_id=viewer_user_id,
    )
    categories = [
        category
        for category in mcp_state.MCP_MARKET_CATEGORIES
        if any(i["category"] == category for i in items)
    ]
    return {"items": items, "categories": categories}


def get_market_item(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    viewer_user_id: Optional[str],
    admin: bool = False,
) -> Dict[str, Any]:
    item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
    if not item or (not admin and item.status != "active"):
        raise ResourceNotFoundError("mcp_market_item", slug)
    if not admin:
        ml.ensure_item_visible(db, ml.KIND_MCP, slug, viewer_user_id, resource="mcp_market_item")
    data = item_dict(
        db,
        item,
        mcp_versions.version_for_item(db, item),
        installed=slug in installed_slug_set(db, owner_user_id),
    )
    state = ml.annotate_and_filter(
        db,
        ml.KIND_MCP,
        [data],
        id_key="slug",
        include_disabled=admin,
        viewer_user_id=viewer_user_id,
    )
    if not state:
        raise ResourceNotFoundError("mcp_market_item", slug)
    return state[0]
