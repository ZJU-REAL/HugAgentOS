"""mcp versions responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import re
import uuid
from typing import Any, Dict, List, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketVersion
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.capabilities.mcp.management import refresh_mcp_caches, tool_snapshot_hash
from sqlalchemy.orm import Session
from features.marketplace import mcp_validation

def version_for_item(db: Session, item: McpMarketItem) -> McpMarketVersion:
    version = (
        db.query(McpMarketVersion)
        .filter(McpMarketVersion.version_id == item.latest_version_id)
        .first()
    )
    if not version:
        raise ResourceNotFoundError("mcp_market_version", item.latest_version_id)
    return version


def publish_snapshot(
    db: Session,
    *,
    slug: str,
    display_name: str,
    description: str,
    user_intro: Optional[str],
    category: str,
    tags: List[str],
    icon: Optional[str],
    publisher_id: Optional[str],
    publisher_name: str,
    source: str,
    version: str,
    transport: str,
    url: str,
    auth_schema: List[Dict[str, Any]],
    auth_config: Optional[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    source_server_id: Optional[str],
    approved_by: Optional[str],
) -> McpMarketItem:
    if (
        db.query(McpMarketVersion)
        .filter(McpMarketVersion.slug == slug, McpMarketVersion.version == version)
        .first()
    ):
        raise BadRequestError(message=f"市场条目 {slug} 已存在版本 {version}，请提升版本号")
    now = utc_now()
    # Include a soft-deleted row so a later reviewed version can resurrect the
    # same stable marketplace slug without colliding on the primary key.
    item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
    version_id = f"mcpver_{uuid.uuid4().hex}"
    if item is None:
        item = McpMarketItem(
            slug=slug,
            display_name=display_name,
            description=description,
            user_intro=user_intro,
            category=mcp_validation.validate_category(category),
            tags=tags,
            icon=icon,
            publisher_id=publisher_id,
            publisher_name=publisher_name,
            source=source,
            latest_version_id=version_id,
            status="active",
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.flush()
    else:
        item.deleted_at = None
        item.display_name = display_name
        item.description = description
        item.user_intro = user_intro
        item.category = mcp_validation.validate_category(category)
        item.tags = tags
        item.icon = icon
        item.publisher_id = publisher_id
        item.publisher_name = publisher_name
        item.latest_version_id = version_id
        item.status = "active"
        item.status_reason = None
        item.last_verified_at = now
        item.updated_at = now
    db.add(
        McpMarketVersion(
            version_id=version_id,
            slug=slug,
            version=mcp_validation.normalize_version(version),
            transport=transport,
            url=url,
            auth_schema=auth_schema,
            auth_config=mcp_validation.normalize_auth_config(auth_config, auth_schema),
            tools_json=tools,
            tool_hash=tool_snapshot_hash(tools),
            source_server_id=source_server_id,
            approved_by=approved_by,
            approved_at=now,
            created_at=now,
        )
    )
    return item


def next_revalidated_version(db: Session, slug: str, current_version: str) -> str:
    """Return a collision-free version for an administrator-accepted snapshot."""
    semantic = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current_version)
    if semantic:
        major, minor, patch = (int(value) for value in semantic.groups())
        candidate = f"{major}.{minor}.{patch + 1}"
        while (
            db.query(McpMarketVersion.version_id)
            .filter(
                McpMarketVersion.slug == slug,
                McpMarketVersion.version == candidate,
            )
            .first()
        ):
            patch += 1
            candidate = f"{major}.{minor}.{patch + 1}"
        return candidate

    base = re.sub(r"[^0-9A-Za-z._+-]", "-", current_version).strip("-.") or "1.0.0"
    sequence = 1
    while True:
        candidate = f"{base[:32]}+recheck.{sequence}"
        if not (
            db.query(McpMarketVersion.version_id)
            .filter(
                McpMarketVersion.slug == slug,
                McpMarketVersion.version == candidate,
            )
            .first()
        ):
            return candidate
        sequence += 1


def version_probe_candidate(item: McpMarketItem, version: McpMarketVersion) -> AdminMcpServer:
    """Build a credential-free probe target from an immutable reviewed version."""
    return AdminMcpServer(
        server_id=f"market_revalidate_{uuid.uuid4().hex}",
        display_name=item.display_name,
        description=item.description or "",
        user_intro=item.user_intro,
        transport=version.transport,
        command=None,
        args=[],
        url=version.url,
        env_vars={},
        env_inherit=[],
        headers={},
        is_enabled=False,
        sort_order=0,
        extra_config={},
        tools_json=list(version.tools_json or []),
        icon=item.icon,
        owner_user_id=None,
        source_plugin=None,
        created_by="market-revalidation",
    )


def accept_revalidated_snapshot(
    db: Session,
    item: McpMarketItem,
    version: McpMarketVersion,
    tools: List[Dict[str, Any]],
) -> McpMarketVersion:
    """Publish live drift as a new immutable version after explicit admin review."""
    next_version = next_revalidated_version(db, item.slug, version.version)
    publish_snapshot(
        db,
        slug=item.slug,
        display_name=item.display_name,
        description=item.description or "",
        user_intro=item.user_intro,
        category=item.category,
        tags=list(item.tags or []),
        icon=item.icon,
        publisher_id=item.publisher_id,
        publisher_name=item.publisher_name or "",
        source=item.source,
        version=next_version,
        transport=version.transport,
        url=version.url,
        auth_schema=list(version.auth_schema or []),
        auth_config=dict(version.auth_config or {}),
        tools=tools,
        source_server_id=version.source_server_id,
        approved_by="admin-revalidation",
    )
    db.flush()
    accepted = version_for_item(db, item)
    if version.source_server_id:
        (
            db.query(McpMarketInstallation)
            .filter(
                McpMarketInstallation.slug == item.slug,
                McpMarketInstallation.server_id == version.source_server_id,
                McpMarketInstallation.version_id == version.version_id,
            )
            .update(
                {
                    McpMarketInstallation.version_id: accepted.version_id,
                    McpMarketInstallation.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
    db.commit()
    refresh_mcp_caches()
    return accepted
