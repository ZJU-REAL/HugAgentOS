"""mcp credentials responsibilities."""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlparse
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketItem, McpMarketVersion
from core.infra.exceptions import BadRequestError
from core.capabilities.mcp.management import decrypt_mcp_headers, mcp_oauth_bundle_storage_key, mcp_query_secret_storage_key, mcp_url_override_storage_key
from sqlalchemy.orm import Session
from features.marketplace import mcp_validation

def credential_storage_key(field: Dict[str, Any]) -> str:
    target = str(field.get("target") or "header")
    name = str(field.get("name") or field.get("key") or "").strip()
    if target == "query":
        return mcp_query_secret_storage_key(name)
    if target == "url":
        return mcp_url_override_storage_key()
    return name


def admin_credential_source(
    db: Session,
    item: McpMarketItem,
    version: McpMarketVersion,
) -> Optional[AdminMcpServer]:
    if item.source != "admin" or not version.source_server_id:
        return None
    return (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == version.source_server_id,
            AdminMcpServer.owner_user_id.is_(None),
        )
        .first()
    )


def admin_managed_credential_headers(
    db: Session,
    item: McpMarketItem,
    version: McpMarketVersion,
    *,
    auth_method: str,
) -> Dict[str, str]:
    """Return an admin publisher's credentials without exposing them to marketplace APIs.

    Marketplace versions remain credential-free.  For an admin-published MCP,
    the original global/source ``AdminMcpServer`` is the encrypted credential
    authority.  If it still has every required field for the selected token
    method, user installations may copy those values into their own encrypted
    server row without asking the user to enter the same token again.
    """
    auth_config = mcp_validation.normalize_auth_config(version.auth_config, list(version.auth_schema or []))
    if auth_config["credential_mode"] == "installer":
        return {}
    selected = next(
        (method for method in auth_config["methods"] if method["id"] == auth_method),
        None,
    )
    if selected is None or selected["type"] != "token":
        return {}
    fields = [
        field
        for field in (version.auth_schema or [])
        if not field.get("methods") or auth_method in field.get("methods", [])
    ]
    if not fields:
        return {}
    source = admin_credential_source(db, item, version)
    if source is None:
        return {}

    stored = decrypt_mcp_headers(source.headers)
    source_query = dict(parse_qsl(urlparse(source.url or "").query, keep_blank_values=True))
    managed: Dict[str, str] = {}
    for field in fields:
        storage_key = credential_storage_key(field)
        value = str(stored.get(storage_key) or "")
        if not value and str(field.get("target") or "header") == "query":
            query_name = str(field.get("name") or field.get("key") or "").strip()
            value = str(source_query.get(query_name) or "")
        if value:
            managed[storage_key] = value
        elif field.get("required", True):
            return {}
    return managed


def materialize_credential_value(field: Dict[str, Any], value: str) -> str:
    prefix = str(field.get("prefix") or "")
    if prefix and not value.lower().startswith(prefix.lower()):
        return f"{prefix}{value}"
    return value


def installation_secrets(
    version: McpMarketVersion,
    credentials: Dict[str, str],
    existing_headers: Dict[str, str] | None = None,
    *,
    auth_method: str,
    oauth_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    stored: Dict[str, str] = {}
    missing: List[str] = []
    for field in version.auth_schema or []:
        method_ids = [str(value) for value in (field.get("methods") or []) if str(value)]
        if method_ids and auth_method not in method_ids:
            continue
        key = str(field.get("key") or "").strip()
        storage_key = credential_storage_key(field)
        if not key or not storage_key:
            continue
        value = str(credentials.get(key) or "").strip()
        if value:
            stored[storage_key] = materialize_credential_value(field, value)
        elif storage_key in (existing_headers or {}):
            stored[storage_key] = str((existing_headers or {})[storage_key])
        elif field.get("required", True):
            missing.append(str(field.get("label") or key))
    if missing:
        raise BadRequestError(message=f"请填写安装凭据：{', '.join(missing)}")
    oauth_key = mcp_oauth_bundle_storage_key()
    if oauth_bundle:
        stored[oauth_key] = json.dumps(oauth_bundle, ensure_ascii=False, separators=(",", ":"))
    elif auth_method == "oauth2" and oauth_key in (existing_headers or {}):
        stored[oauth_key] = str((existing_headers or {})[oauth_key])
    return stored
