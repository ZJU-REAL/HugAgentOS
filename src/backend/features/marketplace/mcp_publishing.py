"""mcp publishing responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketVersion
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from features.marketplace import edition as ml
from core.capabilities.mcp.management import decrypt_mcp_headers, encrypt_mcp_headers, probe_mcp_connectivity, refresh_mcp_caches, validate_remote_mcp_url
from sqlalchemy.orm import Session
from core.db.threads import run_in_session
from features.marketplace import mcp_listing
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

@dataclass(frozen=True)
class Publication:
    """Marketplace listing options for one admin MCP server."""

    category: str
    version: str
    summary: str = ""
    tags: Optional[List[str]] = None
    publisher_name: str = "管理员"
    link_as_installation: bool = True
    auth_schema_override: Optional[List[Dict[str, Any]]] = None
    auth_config_override: Optional[Dict[str, Any]] = None
    allow_deferred_discovery: bool = False


def _source_server(db: Session, source_server_id: str) -> AdminMcpServer:
    row = (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == source_server_id,
            AdminMcpServer.owner_user_id.is_(None),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("admin_mcp_server", source_server_id)
    return row


def _detached_source(db: Session, source_server_id: str) -> AdminMcpServer:
    row = _source_server(db, source_server_id)
    db.expunge(row)
    return row


async def check_publishable(row: AdminMcpServer, publication: Publication) -> List[Dict[str, Any]]:
    """Validate a server for listing (URL policy + live probe); returns its discovered tools."""
    if row.transport not in ("streamable_http", "sse") or not row.url:
        raise BadRequestError(message="MCP 市场仅支持远程 HTTP/SSE 服务；stdio 请通过插件市场分发")
    if (row.extra_config or {}).get("market_slug"):
        raise BadRequestError(message="从 MCP 市场安装的实例不能重复发布")
    await validate_remote_mcp_url(
        row.url,
        allow_private_network=True,
        require_https=False,
    )
    ok, error = await probe_mcp_connectivity(row, validate=True)
    if not ok and not publication.allow_deferred_discovery:
        raise BadRequestError(message=f"MCP 发布检测失败：{error}")
    tools = list(row.tools_json or [])
    if not tools and not publication.allow_deferred_discovery:
        raise BadRequestError(message="MCP 未发现任何工具，不能发布")
    return tools


async def publish_admin_server(source_server_id: str, publication: Publication) -> Dict[str, Any]:
    row = await run_in_session(_detached_source, source_server_id)
    tools = await check_publishable(row, publication)
    return await run_in_session(
        lambda db: record_publication(db, _source_server(db, source_server_id), tools, publication)
    )


def record_publication(
    db: Session, row: AdminMcpServer, tools: List[Dict[str, Any]], publication: Publication
) -> Dict[str, Any]:
    """List a checked server in the marketplace and commit (``row`` belongs to ``db``)."""
    category = publication.category
    version = publication.version
    summary = publication.summary
    tags = publication.tags
    publisher_name = publication.publisher_name
    link_as_installation = publication.link_as_installation
    auth_schema_override = publication.auth_schema_override
    auth_config_override = publication.auth_config_override
    allow_deferred_discovery = publication.allow_deferred_discovery
    if tools:
        row.tools_json = tools
    source_server_id = row.server_id
    existing = (
        db.query(McpMarketItem)
        .join(McpMarketVersion, McpMarketVersion.version_id == McpMarketItem.latest_version_id)
        .filter(McpMarketVersion.source_server_id == source_server_id)
        .first()
    )
    slug = existing.slug if existing else mcp_validation.new_slug(db, row.display_name, source_server_id)
    template_url, inferred_auth_schema = mcp_validation.credential_free_connection(row)
    stored_market_config = dict(row.extra_config or {})
    auth_schema = list(
        auth_schema_override
        if auth_schema_override is not None
        else stored_market_config.get("market_auth_schema") or inferred_auth_schema
    )
    auth_config = mcp_validation.normalize_auth_config(
        (
            auth_config_override
            if auth_config_override is not None
            else stored_market_config.get("market_auth_config")
        ),
        auth_schema,
    )
    item = mcp_versions.publish_snapshot(
        db,
        slug=slug,
        display_name=row.display_name,
        description=(summary or row.description or "").strip(),
        user_intro=row.user_intro,
        category=category,
        tags=list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
        icon=row.icon,
        publisher_id=None,
        publisher_name=publisher_name,
        source="admin",
        version=version,
        transport=row.transport,
        url=template_url,
        auth_schema=auth_schema,
        auth_config=auth_config,
        tools=tools,
        source_server_id=row.server_id,
        approved_by=publisher_name,
    )
    db.flush()
    if allow_deferred_discovery:
        market_version = mcp_versions.version_for_item(db, item)
        market_version.listing_notice = {
            **dict(market_version.listing_notice or {}),
            "discovery_mode": "per_install",
            "install_notice": "该 MCP 的完整工具清单将在用户完成认证后动态发现。",
        }
    # Existing global MCPs are linked as an installation.  A server created
    # through the marketplace-first flow remains a disabled source template
    # until an administrator explicitly installs it from the market.
    if link_as_installation:
        installation = (
            db.query(McpMarketInstallation)
            .filter(McpMarketInstallation.server_id == row.server_id)
            .first()
        )
        if installation is None:
            db.add(
                McpMarketInstallation(
                    install_id=f"mcpins_{uuid.uuid4().hex}",
                    slug=item.slug,
                    version_id=item.latest_version_id,
                    server_id=row.server_id,
                    owner_user_id=None,
                    status="active",
                    installed_by="admin",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        else:
            installation.slug = item.slug
            installation.version_id = item.latest_version_id
            installation.status = "active"
            installation.updated_at = utc_now()
    else:
        for installation in (
            db.query(McpMarketInstallation)
            .filter(McpMarketInstallation.server_id == row.server_id)
            .all()
        ):
            db.delete(installation)
        row.is_enabled = False
        row.extra_config = {
            **dict(row.extra_config or {}),
            "market_source_only": True,
            "market_source_slug": item.slug,
        }
    row.headers = encrypt_mcp_headers(decrypt_mcp_headers(row.headers))
    db.commit()
    ml.set_listing_enabled(db, ml.KIND_MCP, item.slug, True, updated_by=publisher_name)
    refresh_mcp_caches()
    return mcp_listing.item_dict(db, item, mcp_versions.version_for_item(db, item), installed=link_as_installation)
