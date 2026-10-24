"""mcp curated responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
from typing import List
from features.marketplace.mcp_catalog import CURATED_MCP_MARKET_ITEMS
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketVersion
from features.marketplace import edition as ml
from core.capabilities.mcp.management import tool_snapshot_hash
from sqlalchemy.orm import Session
from features.marketplace import mcp_validation

def ensure_curated_market_items(db: Session) -> List[str]:
    """Seed credential-free official templates once, without resurrecting deletions."""
    seeded: List[str] = []
    updated = False
    now = utc_now()
    for definition in CURATED_MCP_MARKET_ITEMS:
        slug = str(definition["slug"])
        # Soft-deleted rows deliberately count as existing: an administrator's
        # removal choice must survive application restarts.
        existing_item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
        if existing_item:
            if existing_item.deleted_at is None:
                existing_version = (
                    db.query(McpMarketVersion)
                    .filter(McpMarketVersion.version_id == existing_item.latest_version_id)
                    .first()
                )
                if existing_version and existing_version.approved_by == "system-curated":
                    schema = list(definition.get("auth_schema") or [])
                    auth_config = mcp_validation.normalize_auth_config(definition.get("auth_config"), schema)
                    notice = dict(existing_version.listing_notice or {})
                    notice.update(
                        {
                            "discovery_mode": "per_install",
                            "install_notice": str(definition.get("install_notice") or ""),
                            "docs_url": str(definition.get("docs_url") or ""),
                        }
                    )
                    description = str(definition.get("description") or "")
                    user_intro = str(definition.get("user_intro") or "")
                    icon = str(definition.get("icon") or "")
                    changed = (
                        list(existing_version.auth_schema or []) != schema
                        or dict(existing_version.auth_config or {}) != auth_config
                        or dict(existing_version.listing_notice or {}) != notice
                        or (existing_item.description or "") != description
                        or (existing_item.user_intro or "") != user_intro
                        or (existing_item.icon or "") != icon
                    )
                    if changed:
                        existing_version.auth_schema = schema
                        existing_version.auth_config = auth_config
                        existing_version.listing_notice = notice
                        existing_item.description = description
                        existing_item.user_intro = user_intro
                        existing_item.icon = icon
                        existing_item.updated_at = now
                        server_ids = [
                            row.server_id
                            for row in db.query(McpMarketInstallation)
                            .filter(McpMarketInstallation.slug == slug)
                            .all()
                        ]
                        if server_ids:
                            (
                                db.query(AdminMcpServer)
                                .filter(AdminMcpServer.server_id.in_(server_ids))
                                .update({AdminMcpServer.icon: icon}, synchronize_session=False)
                            )
                        updated = True
            continue
        tools = list(definition.get("tools") or [])
        listing_notice = {
            "discovery_mode": "per_install",
            "install_notice": str(definition.get("install_notice") or ""),
            "docs_url": str(definition.get("docs_url") or ""),
        }
        version_id = f"mcpver_curated_{slug}"
        item = McpMarketItem(
            slug=slug,
            display_name=str(definition["display_name"]),
            description=str(definition.get("description") or ""),
            user_intro=str(definition.get("user_intro") or ""),
            category=mcp_validation.validate_category(str(definition.get("category") or "通用工具")),
            tags=list(definition.get("tags") or []),
            icon=str(definition.get("icon") or ""),
            publisher_id=None,
            publisher_name=str(definition.get("publisher_name") or "HugAgentOS"),
            source="admin",
            latest_version_id=version_id,
            status="active",
            status_reason=None,
            last_verified_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.add(
            McpMarketVersion(
                version_id=version_id,
                slug=slug,
                version=mcp_validation.normalize_version(str(definition.get("version") or "1.0.0")),
                transport=str(definition.get("transport") or "streamable_http"),
                url=str(definition["url"]),
                auth_schema=list(definition.get("auth_schema") or []),
                auth_config=mcp_validation.normalize_auth_config(
                    definition.get("auth_config"),
                    list(definition.get("auth_schema") or []),
                ),
                tools_json=tools,
                tool_hash=tool_snapshot_hash(tools),
                listing_notice=listing_notice,
                source_server_id=None,
                approved_by="system-curated",
                approved_at=now,
                created_at=now,
            )
        )
        seeded.append(slug)
    if not seeded and not updated:
        return []
    db.commit()
    for slug in seeded:
        ml.set_listing_enabled(db, ml.KIND_MCP, slug, True, updated_by="system-curated")
    return seeded
