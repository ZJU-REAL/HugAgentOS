"""mcp administration responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
from typing import Any, Dict, List, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from features.marketplace import edition as ml
from core.capabilities.mcp.management import decrypt_mcp_headers, encrypt_mcp_headers, refresh_mcp_caches
from sqlalchemy.orm import Session
from features.marketplace import mcp_listing
from features.marketplace import mcp_credentials
from features.marketplace import mcp_oauth_clients
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

def set_market_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    return ml.set_listing_enabled(db, ml.KIND_MCP, slug, enabled, updated_by=updated_by)


def _default_token_contract(credential_mode: Optional[str]):
    auth_schema = [
        {
            "key": "Authorization",
            "label": "Token / Authorization",
            "target": "header",
            "required": True,
            "secret": True,
            "prefix": "Bearer ",
        }
    ]
    auth_config = mcp_validation.normalize_auth_config(
        {
            "default_method": "token",
            "methods": [{"id": "token", "type": "token", "label": "Token"}],
            "credential_mode": credential_mode or "installer",
        },
        auth_schema,
    )
    return auth_schema, auth_config


def update_market_item(
    db: Session,
    slug: str,
    *,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    user_intro: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    icon: Optional[str] = None,
    requires_auth: Optional[bool] = None,
    credential_mode: Optional[str] = None,
    managed_credentials: Optional[Dict[str, str]] = None,
    oauth_registration: Optional[str] = None,
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    oauth_scopes: Optional[List[str]] = None,
    auth_type: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit marketplace presentation metadata and its administrator-owned auth policy.

    Tool schemas, transport, endpoint templates, and version numbers remain
    immutable.  Authentication can be disabled, delegated to each installer,
    or backed by the administrator source server's encrypted credentials.
    Secret values are never persisted in marketplace rows or returned by this
    function.  Presentation fields are propagated to existing installations.
    """
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    version = mcp_versions.version_for_item(db, item)
    previous_auth_schema = list(version.auth_schema or [])

    if display_name is not None:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise BadRequestError(message="MCP 市场名称不能为空")
        item.display_name = normalized_name
    if description is not None:
        item.description = description.strip()
    if user_intro is not None:
        item.user_intro = user_intro.strip() or None
    if category is not None:
        item.category = mcp_validation.validate_category(category)
    if tags is not None:
        item.tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    if icon is not None:
        item.icon = icon.strip() or None

    auth_schema = list(version.auth_schema or [])
    auth_config = mcp_validation.normalize_auth_config(version.auth_config, auth_schema)
    if auth_type == "oauth2" and credential_mode is not None:
        # OAuth tokens are always per installer; only the OAuth App can be shared.
        credential_mode = "installer"
    if requires_auth is False:
        auth_schema = []
        auth_config = mcp_validation.normalize_auth_config(
            {
                "default_method": "none",
                "methods": [{"id": "none", "type": "none", "label": "无需认证"}],
                "credential_mode": "installer",
            },
            auth_schema,
        )
    elif auth_type is not None and not any(
        method["type"] == auth_type for method in auth_config["methods"]
    ):
        # Switching Token <-> OAuth replaces the contract; existing installs keep
        # their old credentials until each user reinstalls.
        if version.approved_by == "system-curated":
            raise BadRequestError(message="精选模板的认证方式由模板定义，不能修改")
        if auth_type == "oauth2":
            auth_schema = []
            auth_config = mcp_validation.normalize_auth_config(
                {
                    "default_method": "oauth2",
                    "methods": [{"id": "oauth2", "type": "oauth2", "label": "OAuth 登录"}],
                    "credential_mode": "installer",
                },
                auth_schema,
            )
        else:
            auth_schema, auth_config = _default_token_contract(credential_mode)
    elif requires_auth is True and not any(
        method["type"] != "none" for method in auth_config["methods"]
    ):
        auth_schema, auth_config = _default_token_contract(credential_mode)

    if credential_mode is not None:
        if credential_mode not in {"installer", "admin"}:
            raise BadRequestError(message="MCP 凭据提供方式无效")
        if credential_mode == "admin" and not auth_schema:
            raise BadRequestError(message="请先勾选需要 Token/Auth")
        auth_config["credential_mode"] = credential_mode

    auth_config = mcp_oauth_clients.apply_oauth_policy(
        db,
        slug,
        auth_config,
        curated=version.approved_by == "system-curated",
        registration=oauth_registration,
        client_id=oauth_client_id,
        client_secret=oauth_client_secret,
        scopes=oauth_scopes,
        updated_by=updated_by,
    )
    version.auth_schema = auth_schema
    version.auth_config = auth_config

    admin_source = None
    if item.source == "admin" and version.source_server_id:
        admin_source = (
            db.query(AdminMcpServer)
            .filter(
                AdminMcpServer.server_id == version.source_server_id,
                AdminMcpServer.owner_user_id.is_(None),
            )
            .first()
        )
        if admin_source is not None:
            admin_source.extra_config = {
                **dict(admin_source.extra_config or {}),
                "market_auth_schema": auth_schema,
                "market_auth_config": auth_config,
            }

    if auth_config["credential_mode"] == "admin":
        if item.source != "admin" or not version.source_server_id:
            raise BadRequestError(message="只有管理员发布的 MCP 才能统一配置 Token")
        if admin_source is None:
            raise BadRequestError(message="管理员发布源已不存在，无法统一配置 Token")
        source_headers = decrypt_mcp_headers(admin_source.headers)
        supplied = dict(managed_credentials or {})
        allowed_keys = {str(field.get("key") or "") for field in auth_schema}
        unknown_keys = sorted(key for key in supplied if key not in allowed_keys)
        if unknown_keys:
            raise BadRequestError(message=f"未知的 MCP 认证字段：{', '.join(unknown_keys)}")
        for field in auth_schema:
            key = str(field.get("key") or "").strip()
            value = str(supplied.get(key) or "").strip()
            if value:
                source_headers[mcp_credentials.credential_storage_key(field)] = mcp_credentials.materialize_credential_value(
                    field, value
                )
        admin_source.headers = encrypt_mcp_headers(source_headers)
        managed_headers = mcp_credentials.admin_managed_credential_headers(
            db,
            item,
            version,
            auth_method=str(auth_config["default_method"]),
        )
        if not managed_headers:
            raise BadRequestError(message="请填写管理员统一 Token/Auth")
    else:
        managed_headers = {}

    item.updated_at = utc_now()

    installation_server_ids = [
        str(row[0])
        for row in db.query(McpMarketInstallation.server_id)
        .filter(McpMarketInstallation.slug == slug)
        .all()
    ]
    installations: List[AdminMcpServer] = []
    if installation_server_ids:
        installations = (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.server_id.in_(installation_server_ids))
            .all()
        )
        for server in installations:
            server.display_name = item.display_name
            server.description = item.description or ""
            server.user_intro = item.user_intro
            server.icon = item.icon
            server.updated_at = item.updated_at
            if requires_auth is False:
                existing_headers = decrypt_mcp_headers(server.headers)
                for field in previous_auth_schema:
                    existing_headers.pop(mcp_credentials.credential_storage_key(field), None)
                server.headers = encrypt_mcp_headers(existing_headers)
            elif managed_headers:
                existing_headers = decrypt_mcp_headers(server.headers)
                existing_headers.update(managed_headers)
                server.headers = encrypt_mcp_headers(existing_headers)

    db.commit()
    refresh_mcp_caches()
    result = mcp_listing.get_market_item(
        db,
        slug,
        owner_user_id=None,
        viewer_user_id=None,
        admin=True,
    )
    result["updated_installations"] = len(installations)
    return result


def delete_market_item(
    db: Session, slug: str, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    version = mcp_versions.version_for_item(db, item)
    if version.source_server_id:
        source = (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.server_id == version.source_server_id)
            .first()
        )
        if source and (source.extra_config or {}).get("market_source_only"):
            db.delete(source)
    item.deleted_at = utc_now()
    item.updated_at = utc_now()
    db.commit()
    ml.set_listing_enabled(db, ml.KIND_MCP, slug, False, updated_by=updated_by)
    return {"slug": slug, "deleted": True}


def set_suspended(
    db: Session,
    slug: str,
    *,
    suspended: bool,
    reason: str = "",
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    now = utc_now()
    item.status = "suspended" if suspended else "active"
    item.status_reason = (reason or "").strip() if suspended else None
    item.updated_at = now
    affected = 0
    installs = db.query(McpMarketInstallation).filter(McpMarketInstallation.slug == slug).all()
    if suspended:
        server_ids = []
        for install in installs:
            install.status = "suspended"
            install.updated_at = now
            server_ids.append(install.server_id)
        if server_ids:
            affected = (
                db.query(AdminMcpServer)
                .filter(AdminMcpServer.server_id.in_(server_ids))
                .update({AdminMcpServer.is_enabled: False}, synchronize_session=False)
            )
    else:
        server_ids = []
        for install in installs:
            if install.status != "suspended":
                continue
            install.status = "active"
            install.updated_at = now
            server_ids.append(install.server_id)
        if server_ids:
            affected = (
                db.query(AdminMcpServer)
                .filter(AdminMcpServer.server_id.in_(server_ids))
                .update({AdminMcpServer.is_enabled: True}, synchronize_session=False)
            )
    db.commit()
    refresh_mcp_caches()
    return {
        "slug": slug,
        "status": item.status,
        "disabled_installations": affected if suspended else 0,
        "restored_installations": affected if not suspended else 0,
    }
