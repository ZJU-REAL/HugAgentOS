"""How this deployment identifies itself to MCP authorization servers.

Three things live here because the install flow, the public routes and the
admin inspector must agree on them byte for byte:

- the browser callback URL, fixed per deployment (no per-flow query) so that a
  client metadata document or an administrator OAuth App can list it exactly;
- the client metadata document (CIMD, SEP-991), whose URL *is* the client_id;
- a read-only inspection of what an MCP server's authorization server offers.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx2
from core.capabilities.mcp.management import validate_remote_mcp_url
from core.config.settings import settings
from core.infra.exceptions import BadRequestError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from pydantic import ValidationError

CALLBACK_PATH = "/v1/mcp-market/oauth/callback"
CLIENT_METADATA_PATH = "/v1/mcp-market/oauth/client-metadata.json"
CLIENT_NAME = "HugAgentOS MCP Client"
_INSPECT_TIMEOUT_S = 10.0


def configured_public_base() -> str:
    """``MCP_OAUTH_PUBLIC_BASE_URL`` after validation, or "" when unset."""
    configured = settings.server.mcp_oauth_public_base_url
    if not configured:
        return ""
    parsed = urlparse(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BadRequestError(message="MCP_OAUTH_PUBLIC_BASE_URL 配置无效")
    return configured


def public_api_base(request: Any) -> str:
    """Browser-reachable API base without reflecting an arbitrary Host header."""
    configured = configured_public_base()
    if configured:
        return configured
    origin = str(request.headers.get("origin") or "").rstrip("/")
    parsed_origin = urlparse(origin)
    if not origin or parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        raise BadRequestError(message="OAuth 登录必须从浏览器发起")
    request_host = str(request.headers.get("host") or request.url.netloc).lower()
    if settings.server.is_prod and parsed_origin.netloc.lower() != request_host:
        raise BadRequestError(
            message="跨域部署必须配置 MCP_OAUTH_PUBLIC_BASE_URL（例如 http(s)://app.example.com/api）"
        )
    return f"{origin}/api"


def callback_url(api_base: str) -> str:
    return f"{api_base}{CALLBACK_PATH}"


def client_metadata_url() -> Optional[str]:
    """The CIMD client_id, or None when this deployment cannot offer one.

    An authorization server fetches the document itself, so its URL must be
    HTTPS and stable.  It is derived only from the configured public base: a
    request's Host header is attacker-influenced and, behind nginx, lacks the
    port and ``/api`` prefix the server would actually fetch.
    """
    base = configured_public_base()
    if not base or urlparse(base).scheme != "https":
        return None
    return f"{base}{CLIENT_METADATA_PATH}"


def client_metadata_document() -> Optional[Dict[str, Any]]:
    client_id = client_metadata_url()
    if client_id is None:
        return None
    return {
        "client_id": client_id,
        "client_name": CLIENT_NAME,
        "redirect_uris": [callback_url(configured_public_base())],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def cimd_unavailable_reason() -> str:
    base = settings.server.mcp_oauth_public_base_url
    if not base:
        return "未配置 MCP_OAUTH_PUBLIC_BASE_URL"
    if urlparse(base).scheme != "https":
        return "MCP_OAUTH_PUBLIC_BASE_URL 不是 HTTPS"
    return ""


async def _get(client: httpx2.AsyncClient, url: str, allow_private_network: bool):
    await validate_remote_mcp_url(url, allow_private_network=allow_private_network, require_https=False)
    return await client.get(url, headers={"Accept": "application/json"})


async def inspect_authorization(server_url: str, *, allow_private_network: bool) -> Dict[str, Any]:
    """Report what the MCP server's authorization server supports, without registering.

    Mirrors the SDK's discovery order (RFC 9728 protected resource metadata,
    then RFC 8414 / OIDC metadata) so the result predicts what an install
    will do.  Failures are reported in ``error`` rather than raised: an
    administrator inspects before publishing and needs the reason.
    """
    result: Dict[str, Any] = {
        "resource_metadata_found": False,
        "authorization_server": "",
        "issuer": "",
        "pkce_s256": False,
        "dcr": False,
        "cimd": False,
        "scopes_supported": [],
        "cimd_usable": client_metadata_url() is not None,
        "cimd_unavailable_reason": cimd_unavailable_reason(),
        "callback_url": callback_url(configured_public_base()) if configured_public_base() else "",
        "recommended_registration": "admin_app",
        "error": "",
    }
    await validate_remote_mcp_url(server_url, allow_private_network=allow_private_network, require_https=False)
    async with httpx2.AsyncClient(timeout=_INSPECT_TIMEOUT_S, follow_redirects=False) as client:
        try:
            probe = await client.post(
                server_url,
                json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            www_auth = extract_resource_metadata_from_www_auth(probe) if probe.status_code in (401, 403) else None
            auth_server: Optional[str] = None
            for url in build_protected_resource_metadata_discovery_urls(www_auth, server_url):
                response = await _get(client, url, allow_private_network)
                if response.status_code != 200:
                    continue
                try:
                    resource = ProtectedResourceMetadata.model_validate_json(response.content)
                except ValidationError:
                    continue
                result["resource_metadata_found"] = True
                if resource.authorization_servers:
                    auth_server = str(resource.authorization_servers[0])
                break
            result["authorization_server"] = auth_server or ""
            metadata: Optional[OAuthMetadata] = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server, server_url):
                response = await _get(client, url, allow_private_network)
                if response.status_code == 200:
                    try:
                        metadata = OAuthMetadata.model_validate_json(response.content)
                    except ValidationError:
                        metadata = None
                    break
                if response.status_code >= 500:
                    break
        except BadRequestError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface the reason to the inspecting admin
            result["error"] = f"无法连接授权服务器：{type(exc).__name__}"
            return result
    if metadata is None:
        result["error"] = "未找到 OAuth 授权服务器元数据；该 MCP 可能不支持 OAuth"
        return result
    result["issuer"] = str(metadata.issuer)
    result["authorization_server"] = result["authorization_server"] or str(metadata.issuer)
    result["pkce_s256"] = "S256" in (metadata.code_challenge_methods_supported or [])
    result["dcr"] = metadata.registration_endpoint is not None
    result["cimd"] = metadata.client_id_metadata_document_supported is True
    result["scopes_supported"] = list(metadata.scopes_supported or [])
    if result["cimd"] and result["cimd_usable"]:
        result["recommended_registration"] = "auto"
    elif result["dcr"]:
        result["recommended_registration"] = "auto"
    return result
