"""mcp installation responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import uuid
from typing import Any, Dict, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from features.marketplace import edition as ml
from core.capabilities.mcp.management import decrypt_mcp_headers, encrypt_mcp_headers, materialize_mcp_http_connection, probe_mcp_connectivity, refresh_mcp_caches, tool_snapshot_hash, validate_remote_mcp_url
from sqlalchemy.orm import Session
from core.db.threads import run_in_session
from features.marketplace import mcp_credentials
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

def _plan_installation(
    db: Session,
    slug: str,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    credentials: Optional[Dict[str, str]],
    auth_method: Optional[str],
    oauth_bundle: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve the version, credentials and target server; returns a transient candidate row."""
    item = (
        db.query(McpMarketItem)
        .filter(
            McpMarketItem.slug == slug,
            McpMarketItem.deleted_at.is_(None),
            McpMarketItem.status == "active",
        )
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    if owner_user_id is not None:
        if slug in ml.get_disabled_ids(db, ml.KIND_MCP):
            raise ResourceNotFoundError("mcp_market_item", slug)
        ml.ensure_item_visible(db, ml.KIND_MCP, slug, owner_user_id, resource="mcp_market_item")
    version = mcp_versions.version_for_item(db, item)
    auth_config = mcp_validation.normalize_auth_config(version.auth_config, list(version.auth_schema or []))
    selected_method = str(auth_method or auth_config["default_method"])
    selected = next(
        (method for method in auth_config["methods"] if method["id"] == selected_method),
        None,
    )
    if selected is None:
        raise BadRequestError(message="不支持所选 MCP 认证方式")
    if selected["type"] == "oauth2" and not oauth_bundle:
        raise BadRequestError(message="请先完成 OAuth 登录")
    if selected["type"] != "oauth2" and oauth_bundle:
        raise BadRequestError(message="当前认证方式不能写入 OAuth 凭据")
    existing = db.query(McpMarketInstallation).filter(McpMarketInstallation.slug == slug)
    existing = (
        existing.filter(McpMarketInstallation.owner_user_id.is_(None))
        if owner_user_id is None
        else existing.filter(McpMarketInstallation.owner_user_id == owner_user_id)
    ).first()
    existing_server = (
        db.query(AdminMcpServer).filter(AdminMcpServer.server_id == existing.server_id).first()
        if existing
        else None
    )
    if existing_server is None and owner_user_id is None and version.source_server_id:
        source_candidate = (
            db.query(AdminMcpServer)
            .filter(
                AdminMcpServer.server_id == version.source_server_id,
                AdminMcpServer.owner_user_id.is_(None),
            )
            .first()
        )
        if source_candidate and (source_candidate.extra_config or {}).get("market_source_only"):
            existing_server = source_candidate
    old_headers = decrypt_mcp_headers(existing_server.headers) if existing_server else {}
    managed_headers = mcp_credentials.admin_managed_credential_headers(
        db,
        item,
        version,
        auth_method=selected_method,
    )
    fallback_headers = {**managed_headers, **old_headers}
    headers = mcp_credentials.installation_secrets(
        version,
        dict(credentials or {}),
        fallback_headers,
        auth_method=selected_method,
        oauth_bundle=oauth_bundle,
    )
    runtime_url, _ = materialize_mcp_http_connection(version.url, headers)
    now = utc_now()
    server_id = existing_server.server_id if existing_server else f"mmcp_{uuid.uuid4().hex[:20]}"
    candidate = AdminMcpServer(
        server_id=server_id,
        display_name=item.display_name,
        description=item.description or "",
        user_intro=item.user_intro,
        transport=version.transport,
        command=None,
        args=[],
        url=version.url,
        env_vars={},
        env_inherit=[],
        headers=headers,
        is_enabled=True,
        sort_order=0,
        extra_config={
            **(
                {
                    key: value
                    for key, value in dict(existing_server.extra_config or {}).items()
                    if key not in {"market_source_only", "market_source_slug"}
                }
                if existing_server
                else {}
            ),
            "market_slug": slug,
            "market_version": version.version,
            "listing_notice": dict(version.listing_notice or {}),
            "auth_method": selected_method,
        },
        tools_json=list(version.tools_json or []),
        icon=item.icon,
        owner_user_id=owner_user_id,
        source_plugin=None,
        created_by=installed_by,
        created_at=existing_server.created_at if existing_server else now,
        updated_at=now,
    )

    return {
        "candidate": candidate,
        "headers": headers,
        "runtime_url": runtime_url,
        "allow_private_network": mcp_validation.installs_admin_published_endpoint(
            item, version, runtime_url
        )
        or mcp_validation.self_service_private_network_allowed(),
        "item_id": item.slug,
        "version_id": version.version_id,
        "install_id": existing.install_id if existing else None,
        "existing_server_id": existing_server.server_id if existing_server else None,
        "now": now,
    }


async def install_market_item(
    slug: str,
    *,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    credentials: Optional[Dict[str, str]] = None,
    auth_method: Optional[str] = None,
    oauth_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Install a marketplace MCP: plan, probe the live server, then commit.

    The URL check and probe run between two database steps, on a transient
    candidate row, so nothing is held open while the remote server answers.
    """
    plan = await run_in_session(
        _plan_installation, slug, owner_user_id, installed_by, credentials, auth_method, oauth_bundle
    )
    await validate_remote_mcp_url(
        plan["runtime_url"],
        allow_private_network=plan["allow_private_network"],
        require_https=False,
    )
    ok, error = await probe_mcp_connectivity(plan["candidate"], validate=True)
    if not ok:
        raise BadRequestError(message=f"MCP 安装检测失败：{error}")
    result = await run_in_session(_commit_installation, slug, owner_user_id, installed_by, plan)
    refresh_mcp_caches()
    return result


def _commit_installation(
    db: Session,
    slug: str,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    from features.marketplace.models import McpMarketVersion

    candidate, headers, now = plan["candidate"], plan["headers"], plan["now"]
    item = db.query(McpMarketItem).filter(McpMarketItem.slug == plan["item_id"]).one()
    version = db.get(McpMarketVersion, plan["version_id"])
    existing = db.get(McpMarketInstallation, plan["install_id"]) if plan["install_id"] else None
    existing_server = (
        db.query(AdminMcpServer).filter(AdminMcpServer.server_id == plan["existing_server_id"]).first()
        if plan["existing_server_id"]
        else None
    )
    server_id = candidate.server_id
    discovery_mode = str((version.listing_notice or {}).get("discovery_mode") or "reviewed")
    if discovery_mode == "per_install":
        candidate.extra_config = {
            **dict(candidate.extra_config or {}),
            "discovery_mode": discovery_mode,
        }
    else:
        actual_hash = tool_snapshot_hash(candidate.tools_json or [])
        if actual_hash != version.tool_hash:
            item.status = "changed"
            item.status_reason = "远程工具或参数结构已变化，等待管理员重新审核"
            item.updated_at = now
            db.commit()
            raise BadRequestError(message="远程 MCP 工具清单已发生变化，已暂停安装并等待重新审核")

    server = existing_server or AdminMcpServer(server_id=server_id)
    for field in (
        "display_name",
        "description",
        "user_intro",
        "transport",
        "command",
        "args",
        "url",
        "env_vars",
        "env_inherit",
        "is_enabled",
        "sort_order",
        "extra_config",
        "tools_json",
        "icon",
        "owner_user_id",
        "source_plugin",
        "created_by",
        "updated_at",
    ):
        setattr(server, field, getattr(candidate, field))
    server.headers = encrypt_mcp_headers(headers)
    if not existing_server:
        server.created_at = now
        db.add(server)
        db.flush()
    if existing:
        previous_version_id = existing.version_id
        existing.version_id = version.version_id
        existing.status = "active"
        existing.installed_by = installed_by
        existing.updated_at = now
        action = "updated" if previous_version_id != version.version_id else "existing"
    else:
        existing = McpMarketInstallation(
            install_id=f"mcpins_{uuid.uuid4().hex}",
            slug=slug,
            version_id=version.version_id,
            server_id=server.server_id,
            owner_user_id=owner_user_id,
            status="active",
            installed_by=installed_by,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
        action = "installed"
    db.commit()
    return {
        "install_id": existing.install_id,
        "server_id": server.server_id,
        "slug": slug,
        "version": version.version,
        "action": action,
    }
