"""mcp validation responsibilities."""
from __future__ import annotations
import re
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketItem, McpMarketSubmission, McpMarketVersion
from core.infra.exceptions import BadRequestError
from core.capabilities.mcp.management import auth_schema_from_headers
from sqlalchemy.orm import Session
from features.marketplace import mcp_oauth_clients
from features.marketplace import mcp_state

def validate_category(category: str) -> str:
    value = (category or "").strip()
    if value not in mcp_state.MCP_MARKET_CATEGORIES:
        raise BadRequestError(message=f"MCP 市场分类必须是：{', '.join(mcp_state.MCP_MARKET_CATEGORIES)}")
    return value


def normalize_auth_config(
    auth_config: Any,
    auth_schema: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a backwards-compatible none/token/oauth2 authentication contract."""
    config = dict(auth_config or {}) if isinstance(auth_config, dict) else {}
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in config.get("methods") or []:
        if not isinstance(raw, dict):
            continue
        method = dict(raw)
        method_id = str(method.get("id") or method.get("type") or "").strip()
        method_type = str(method.get("type") or "").strip()
        if not method_id or method_id in seen or method_type not in {"none", "token", "oauth2"}:
            continue
        method["id"] = method_id
        method["type"] = method_type
        method["label"] = str(method.get("label") or method_type)
        if method_type == "oauth2":
            method["client_registration"] = mcp_oauth_clients.normalize_registration(
                method.get("client_registration")
            )
            method["scopes"] = [str(scope) for scope in (method.get("scopes") or []) if str(scope)]
        normalized.append(method)
        seen.add(method_id)
    if not normalized:
        inferred = "token" if auth_schema else "none"
        normalized = [
            {"id": inferred, "type": inferred, "label": "Token" if auth_schema else "无需认证"}
        ]
    default_method = str(config.get("default_method") or normalized[0]["id"])
    if default_method not in {method["id"] for method in normalized}:
        default_method = str(normalized[0]["id"])
    credential_mode = str(config.get("credential_mode") or "auto")
    if credential_mode not in {"auto", "installer", "admin"}:
        credential_mode = "auto"
    return {
        "default_method": default_method,
        "methods": normalized,
        "credential_mode": credential_mode,
    }


def endpoint_identity(url: str) -> tuple[str, str, int | None]:
    """Scheme/host/port of an endpoint, ignoring path, query, and credentials."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "").lower()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").lower(), parsed.port or default_port


def self_service_private_network_allowed() -> bool:
    """Whether this trusted private deployment lets user-supplied endpoints reach its LAN.

    The same switch that admits a user's private MCP must also admit its
    marketplace submission, review, installs and revalidation; otherwise an
    intranet MCP can be connected but never listed.
    """
    from core.config.settings import settings

    return settings.server.mcp_self_service_allow_private_network


def installs_admin_published_endpoint(
    item: McpMarketItem,
    version: McpMarketVersion,
    runtime_url: str,
) -> bool:
    """Whether an install still targets the exact endpoint an admin put on the shelf.

    Admin publishing already vets the endpoint and may legitimately point at the
    deployment's own network (the built-in MCP container, a LAN service).  The
    SSRF boundary exists for endpoints the *installer* supplies, so it must key
    on the endpoint's provenance rather than on who is installing: an unchanged
    admin-published host stays trusted for everyone who installs it, while a
    URL-target credential that redirects the install elsewhere is checked
    strictly, whoever submits it.
    """
    if item.source != "admin":
        return False
    return endpoint_identity(runtime_url) == endpoint_identity(version.url)


def credential_free_connection(row: AdminMcpServer) -> tuple[str, List[Dict[str, Any]]]:
    """Strip likely query-string secrets and expose only their install-time schema."""
    parsed = urlparse(row.url or "")
    public_query: List[tuple[str, str]] = []
    auth_schema = auth_schema_from_headers(row.headers)
    known_keys = {str(field.get("key") or "") for field in auth_schema}
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
        looks_secret = any(term in name.lower() for term in mcp_state.SECRET_QUERY_TERMS)
        credential_key = f"QUERY_{normalized or 'SECRET'}"
        if looks_secret and value:
            if credential_key not in known_keys:
                auth_schema.append(
                    {
                        "key": credential_key,
                        "label": name,
                        "target": "query",
                        "name": name,
                        "required": True,
                        "secret": True,
                    }
                )
                known_keys.add(credential_key)
            continue
        public_query.append((name, value))
    template_url = urlunparse(parsed._replace(query=urlencode(public_query)))
    return template_url, auth_schema


def normalize_version(version: str) -> str:
    value = (version or "1.0.0").strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,49}", value):
        raise BadRequestError(message="版本号格式无效")
    return value


def new_slug(db: Session, display_name: str, source_server_id: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    base = (ascii_slug or f"mcp-{source_server_id[-12:].lower()}")[:100]
    slug = base
    suffix = 2
    while (
        db.query(McpMarketItem.slug).filter(McpMarketItem.slug == slug).first()
        or db.query(McpMarketSubmission.slug)
        .filter(
            McpMarketSubmission.slug == slug,
            McpMarketSubmission.status.in_(["pending", "approved"]),
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    ):
        slug = f"{base[:115]}-{suffix}"
        suffix += 1
    return slug


def source_slug(db: Session, row: AdminMcpServer, owner_user_id: str) -> str:
    previous = (
        db.query(McpMarketSubmission.slug)
        .filter(
            McpMarketSubmission.source_server_id == row.server_id,
            McpMarketSubmission.owner_user_id == owner_user_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .order_by(McpMarketSubmission.created_at.desc())
        .first()
    )
    return str(previous[0]) if previous else new_slug(db, row.display_name, row.server_id)


def safe_origin(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
