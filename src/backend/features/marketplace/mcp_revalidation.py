"""mcp revalidation responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
from typing import Any, Dict
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketItem
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.capabilities.mcp.management import probe_mcp_connectivity, tool_snapshot_hash, validate_remote_mcp_url
from sqlalchemy.orm import Session
from features.marketplace import mcp_administration
from features.marketplace import mcp_listing
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

async def revalidate_market_item(
    db: Session,
    slug: str,
    *,
    accept_changes: bool = False,
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    version = mcp_versions.version_for_item(db, item)
    if str((version.listing_notice or {}).get("discovery_mode") or "") == "per_install":
        # Curated templates have no shared provider credential, so their live
        # tools are intentionally discovered with each installer's credential.
        # Revalidation checks only a fixed public endpoint; private endpoint
        # templates are validated when the user installs them.
        has_endpoint_override = any(
            str(field.get("target") or "") == "url" for field in version.auth_schema or []
        )
        if not has_endpoint_override:
            try:
                await validate_remote_mcp_url(version.url, require_https=False)
                item.status = "active"
                item.status_reason = None
            except BadRequestError as exc:
                item.status = "suspended"
                item.status_reason = f"官方端点安全复检失败：{exc}"
        else:
            item.status = "active"
            item.status_reason = "个人端点将在安装时使用用户凭据验证"
        item.last_verified_at = utc_now()
        item.updated_at = utc_now()
        db.commit()
        return mcp_listing.item_dict(db, item, version, installed=False)
    source = (
        db.query(AdminMcpServer)
        .filter(AdminMcpServer.server_id == version.source_server_id)
        .first()
        if version.source_server_id
        else None
    )
    if source:
        source_template_url, source_auth_schema = mcp_validation.credential_free_connection(source)
        if (
            source.transport != version.transport
            or source_template_url.strip() != version.url.strip()
        ):
            item.status = "changed"
            item.status_reason = "原始 MCP 的连接地址或传输方式已变化，需发布新版本"
            item.last_verified_at = utc_now()
            item.updated_at = utc_now()
            db.commit()
            return mcp_listing.item_dict(db, item, version, installed=False)
        auth_policy = mcp_validation.normalize_auth_config(version.auth_config, list(version.auth_schema or []))
        if auth_policy["credential_mode"] == "auto" and source_auth_schema != list(
            version.auth_schema or []
        ):
            item.status = "changed"
            item.status_reason = "原始 MCP 的认证参数已变化，需发布新版本"
            item.last_verified_at = utc_now()
            item.updated_at = utc_now()
            db.commit()
            return mcp_listing.item_dict(db, item, version, installed=False)
    try:
        await validate_remote_mcp_url(
            version.url,
            allow_private_network=item.source == "admin",
            require_https=False,
        )
    except BadRequestError as exc:
        mcp_administration.set_suspended(db, slug, suspended=True, reason=f"远程地址安全复检失败：{exc}")
        return mcp_listing.item_dict(db, item, version, installed=False)
    probe_target = source or mcp_versions.version_probe_candidate(item, version)
    ok, error = await probe_mcp_connectivity(probe_target, db if source else None)
    discovered_tools = list(probe_target.tools_json or [])
    if not ok:
        item.status = "changed"
        prefix = "复检连接失败" if source else "原始 MCP 连接已不存在，版本端点复检失败"
        item.status_reason = f"{prefix}：{error}"
    elif not discovered_tools:
        item.status = "changed"
        item.status_reason = "复检连接成功，但远程 MCP 未返回任何工具，暂不接受新快照"
    else:
        actual_hash = tool_snapshot_hash(discovered_tools)
        if actual_hash == version.tool_hash:
            item.status = "active"
            item.status_reason = None
        elif accept_changes and item.source == "admin":
            previous_version = version.version
            accepted = mcp_versions.accept_revalidated_snapshot(db, item, version, discovered_tools)
            result = mcp_listing.item_dict(db, item, accepted, installed=False)
            result["snapshot_accepted"] = True
            result["previous_version"] = previous_version
            return result
        else:
            item.status = "changed"
            item.status_reason = "远程工具或参数结构已变化，需发布新版本并重新审核"
    item.last_verified_at = utc_now()
    item.updated_at = utc_now()
    db.commit()
    return mcp_listing.item_dict(db, item, version, installed=False)
