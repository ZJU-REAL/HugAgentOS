"""Provider-neutral OAuth 2.1 flow for remote MCP marketplace installs.

The implementation delegates MCP protected-resource discovery, authorization
server metadata, PKCE, DCR, resource indicators, token exchange, and refresh to
the official Python MCP SDK.  Marketplace metadata remains credential-free;
the resulting bundle is encrypted inside the concrete installation only.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

from core.config.settings import settings
from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer, McpMarketItem, McpMarketVersion
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.crypto import decrypt_secret, encrypt_secret
from core.infra.redis import get_redis
from core.services.mcp_management_service import (
    decrypt_mcp_headers,
    encrypt_mcp_headers,
    mcp_oauth_bundle_storage_key,
    probe_mcp_connectivity,
    validate_remote_mcp_url,
)
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from sqlalchemy.orm import Session

_FLOW_TTL_SECONDS = 10 * 60


class OAuthBundleStorage(TokenStorage):
    """SDK token storage backed by an encrypted MCP installation row."""

    def __init__(self, bundle: Optional[Dict[str, Any]] = None, *, server_id: Optional[str] = None):
        self.bundle: Dict[str, Any] = dict(bundle or {})
        self.server_id = server_id

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.bundle.get("tokens")
        return OAuthToken.model_validate(raw) if isinstance(raw, dict) else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        previous = self.bundle.get("tokens") if isinstance(self.bundle.get("tokens"), dict) else {}
        if not tokens.refresh_token and previous.get("refresh_token"):
            tokens = tokens.model_copy(update={"refresh_token": previous["refresh_token"]})
        self.bundle["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self.bundle["expires_at"] = (
            time.time() + int(tokens.expires_in)
            if tokens.expires_in is not None
            else None
        )
        self._persist()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.bundle.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if isinstance(raw, dict) else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.bundle["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._persist()

    def capture_context(self, provider: OAuthClientProvider) -> Dict[str, Any]:
        context = provider.context
        self.bundle["server_url"] = context.server_url
        self.bundle["client_metadata"] = context.client_metadata.model_dump(
            mode="json", exclude_none=True
        )
        self.bundle["oauth_metadata"] = (
            context.oauth_metadata.model_dump(mode="json", exclude_none=True)
            if context.oauth_metadata
            else None
        )
        self.bundle["protected_resource_metadata"] = (
            context.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
            if context.protected_resource_metadata
            else None
        )
        self.bundle["auth_server_url"] = context.auth_server_url
        self.bundle["protocol_version"] = context.protocol_version
        self._persist()
        return dict(self.bundle)

    def _persist(self) -> None:
        if not self.server_id:
            return
        db = SessionLocal()
        try:
            row = db.query(AdminMcpServer).filter(AdminMcpServer.server_id == self.server_id).first()
            if not row:
                return
            headers = decrypt_mcp_headers(row.headers)
            headers[mcp_oauth_bundle_storage_key()] = json.dumps(
                self.bundle, ensure_ascii=False, separators=(",", ":")
            )
            row.headers = encrypt_mcp_headers(headers)
            db.commit()
        finally:
            db.close()


def build_oauth_provider(
    server_url: str,
    bundle: Dict[str, Any],
    *,
    storage: Optional[OAuthBundleStorage] = None,
    redirect_handler=None,
    callback_handler=None,
) -> OAuthClientProvider:
    """Rehydrate an SDK provider for first login or automatic refresh."""
    actual_storage = storage or OAuthBundleStorage(bundle)
    metadata_raw = bundle.get("client_metadata") or {
        "redirect_uris": ["http://localhost/oauth/callback"],
        "client_name": "HugAgentOS MCP Client",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    provider = OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata.model_validate(metadata_raw),
        storage=actual_storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=300.0,
    )
    async def _validate_oauth_request(request) -> None:
        # OAuth discovery metadata is controlled by the remote resource.  Apply
        # the same DNS/IP SSRF boundary to every SDK-generated request, not only
        # to the original MCP URL.
        await validate_remote_mcp_url(str(request.url), require_https=True)

    provider.mcp_request_hook = _validate_oauth_request
    context = provider.context
    if isinstance(bundle.get("oauth_metadata"), dict):
        context.oauth_metadata = OAuthMetadata.model_validate(bundle["oauth_metadata"])
    if isinstance(bundle.get("protected_resource_metadata"), dict):
        context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
            bundle["protected_resource_metadata"]
        )
    context.auth_server_url = bundle.get("auth_server_url")
    context.protocol_version = bundle.get("protocol_version")
    expires_at = bundle.get("expires_at")
    context.token_expiry_time = float(expires_at) if expires_at else None
    return provider


@dataclass
class OAuthInstallFlow:
    flow_id: str
    slug: str
    owner_user_id: Optional[str]
    installed_by: Optional[str]
    auth_method: str
    credentials: Dict[str, str]
    confirm_high_risk: bool
    callback_url: str
    client_id: str = ""
    client_secret: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "starting"
    authorization_url: str = ""
    error: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    callback_code: str = ""
    callback_state: Optional[str] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    callback_received: asyncio.Event = field(default_factory=asyncio.Event)


_flows: Dict[str, OAuthInstallFlow] = {}


def _flow_key(flow_id: str) -> str:
    return f"mcp:oauth:flow:{flow_id}"


def _callback_key(flow_id: str) -> str:
    return f"mcp:oauth:callback:{flow_id}"


async def _store_flow_status(flow: OAuthInstallFlow) -> None:
    payload = {
        "flow_id": flow.flow_id,
        "slug": flow.slug,
        "owner_user_id": flow.owner_user_id,
        "status": flow.status,
        "error": flow.error,
        "result": dict(flow.result or {}),
    }
    await get_redis().set(
        _flow_key(flow.flow_id),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ex=_FLOW_TTL_SECONDS,
    )


def public_oauth_callback_url(request: Any) -> str:
    """Build a trusted browser callback without reflecting an arbitrary Host header."""
    configured = settings.server.mcp_oauth_public_base_url
    if configured:
        parsed = urlparse(configured)
        if (
            parsed.scheme not in ({"https"} if settings.server.is_prod else {"http", "https"})
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BadRequestError(message="MCP_OAUTH_PUBLIC_BASE_URL 配置无效")
        return f"{configured}/v1/mcp-market/oauth/callback"

    origin = str(request.headers.get("origin") or "").rstrip("/")
    parsed_origin = urlparse(origin)
    if not origin or parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        raise BadRequestError(message="OAuth 登录必须从浏览器发起")
    request_host = str(request.headers.get("host") or request.url.netloc).lower()
    if settings.server.is_prod and parsed_origin.netloc.lower() != request_host:
        raise BadRequestError(
            message="跨域部署必须配置 MCP_OAUTH_PUBLIC_BASE_URL（例如 https://app.example.com/api）"
        )
    return f"{origin}/api/v1/mcp-market/oauth/callback"


def _purge_expired_flows() -> None:
    cutoff = time.time() - _FLOW_TTL_SECONDS
    for flow_id, flow in list(_flows.items()):
        if flow.created_at < cutoff:
            _flows.pop(flow_id, None)


def _get_version(db: Session, slug: str) -> tuple[McpMarketItem, McpMarketVersion]:
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
    version = (
        db.query(McpMarketVersion)
        .filter(McpMarketVersion.version_id == item.latest_version_id)
        .first()
    )
    if not version:
        raise ResourceNotFoundError("mcp_market_version", item.latest_version_id)
    return item, version


async def start_oauth_install(
    db: Session,
    *,
    slug: str,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    auth_method: str,
    credentials: Dict[str, str],
    confirm_high_risk: bool,
    callback_url_base: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, str]:
    """Start a browser OAuth install and wait only until the authorize URL is known."""
    from core.services import mcp_marketplace_service as market

    _purge_expired_flows()
    _, version = _get_version(db, slug)
    auth_config = market._normalize_auth_config(version.auth_config, list(version.auth_schema or []))
    method = next((row for row in auth_config["methods"] if row["id"] == auth_method), None)
    if not method or method["type"] != "oauth2":
        raise BadRequestError(message="该 MCP 不支持所选 OAuth 认证方式")
    if method.get("client_id_required") and not client_id.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client ID")
    if method.get("client_secret_required") and not client_secret.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client Secret")
    if version.risk_level == "high" and not confirm_high_risk:
        raise BadRequestError(message="该 MCP 包含高风险操作，OAuth 登录前必须明确确认风险")

    flow_id = f"mcpoauth_{uuid.uuid4().hex}"
    callback_url = f"{callback_url_base}?{urlencode({'flow_id': flow_id})}"
    flow = OAuthInstallFlow(
        flow_id=flow_id,
        slug=slug,
        owner_user_id=owner_user_id,
        installed_by=installed_by,
        auth_method=auth_method,
        credentials=dict(credentials or {}),
        confirm_high_risk=confirm_high_risk,
        callback_url=callback_url,
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
    )
    _flows[flow_id] = flow
    await _store_flow_status(flow)
    asyncio.create_task(_run_flow(flow), name=f"mcp-oauth-{flow_id}")
    try:
        await asyncio.wait_for(flow.ready.wait(), timeout=30.0)
    except asyncio.TimeoutError as exc:
        flow.status = "failed"
        flow.error = "OAuth 元数据发现超时"
        await _store_flow_status(flow)
        raise BadRequestError(message=flow.error) from exc
    if not flow.authorization_url:
        raise BadRequestError(message=flow.error or "无法启动 OAuth 登录")
    return {
        "flow_id": flow.flow_id,
        "authorization_url": flow.authorization_url,
        "status": flow.status,
    }


async def _run_flow(flow: OAuthInstallFlow) -> None:
    from core.services import mcp_marketplace_service as market

    db = SessionLocal()
    try:
        item, version = _get_version(db, flow.slug)
        auth_config = market._normalize_auth_config(version.auth_config, list(version.auth_schema or []))
        method = next(row for row in auth_config["methods"] if row["id"] == flow.auth_method)
        headers = market._installation_secrets(
            version,
            flow.credentials,
            {},
            auth_method=flow.auth_method,
        )
        runtime_url, _ = market.materialize_mcp_http_connection(version.url, headers)
        await validate_remote_mcp_url(runtime_url, require_https=True)

        metadata = OAuthClientMetadata(
            redirect_uris=[flow.callback_url],
            token_endpoint_auth_method=("client_secret_post" if flow.client_secret else "none"),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(method.get("scopes") or []) or None,
            client_name="HugAgentOS MCP Client",
        )
        storage = OAuthBundleStorage(
            {
                "server_url": runtime_url,
                "client_metadata": metadata.model_dump(mode="json", exclude_none=True),
            }
        )
        if flow.client_id:
            await storage.set_client_info(
                OAuthClientInformationFull(
                    **metadata.model_dump(mode="json", exclude_none=True),
                    client_id=flow.client_id,
                    client_secret=flow.client_secret or None,
                )
            )

        async def redirect_handler(url: str) -> None:
            flow.authorization_url = url
            flow.status = "waiting_for_user"
            await _store_flow_status(flow)
            flow.ready.set()

        async def callback_handler() -> tuple[str, str | None]:
            deadline = time.monotonic() + 300.0
            while time.monotonic() < deadline:
                if flow.callback_received.is_set():
                    if flow.error:
                        raise RuntimeError(flow.error)
                    return flow.callback_code, flow.callback_state
                raw = await get_redis().get(_callback_key(flow.flow_id))
                if raw:
                    payload = json.loads(raw)
                    callback_error = str(payload.get("error") or "")
                    if callback_error:
                        raise RuntimeError(callback_error)
                    encrypted_code = str(payload.get("code") or "")
                    callback_code = decrypt_secret(encrypted_code) if encrypted_code else ""
                    return callback_code or "", payload.get("state")
                await asyncio.sleep(0.5)
            raise TimeoutError("OAuth 登录回调超时")

        provider = build_oauth_provider(
            runtime_url,
            storage.bundle,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        candidate = AdminMcpServer(
            server_id=f"oauth_probe_{uuid.uuid4().hex[:16]}",
            display_name=item.display_name,
            description=item.description or "",
            transport=version.transport,
            url=version.url,
            headers=headers,
            args=[],
            env_vars={},
            env_inherit=[],
            is_stable=False,
            is_enabled=False,
            extra_config={},
        )
        ok, error = await probe_mcp_connectivity(
            candidate,
            db,
            oauth_provider=provider,
            timeout_seconds=300.0,
        )
        if not ok:
            raise RuntimeError(f"OAuth MCP 连接失败：{error}")
        if flow.error:
            raise RuntimeError(flow.error)
        persisted = await get_redis().get(_flow_key(flow.flow_id))
        if persisted:
            persisted_status = json.loads(persisted)
            if persisted_status.get("status") == "failed":
                raise RuntimeError(str(persisted_status.get("error") or "OAuth 登录已取消"))
        bundle = storage.capture_context(provider)
        flow.status = "installing"
        await _store_flow_status(flow)
        flow.result = await market.install_market_item(
            db,
            flow.slug,
            owner_user_id=flow.owner_user_id,
            installed_by=flow.installed_by,
            credentials=flow.credentials,
            auth_method=flow.auth_method,
            oauth_bundle=bundle,
            confirm_high_risk=flow.confirm_high_risk,
        )
        flow.status = "completed"
        await _store_flow_status(flow)
    except BaseException as exc:  # noqa: BLE001
        db.rollback()
        flow.status = "failed"
        flow.error = str(exc)
        await _store_flow_status(flow)
    finally:
        flow.ready.set()
        db.close()


async def complete_callback(
    flow_id: str,
    *,
    code: str = "",
    state: Optional[str] = None,
    error: str = "",
) -> None:
    flow = _flows.get(flow_id)
    public_raw = await get_redis().get(_flow_key(flow_id))
    if not flow and not public_raw:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    await get_redis().set(
        _callback_key(flow_id),
        json.dumps(
            {
                "code": encrypt_secret(code) if code else "",
                "state": state,
                "error": error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        ex=_FLOW_TTL_SECONDS,
    )
    if flow:
        flow.callback_code = code
        flow.callback_state = state
        flow.error = error
        flow.status = "failed" if error else "processing_callback"
        flow.callback_received.set()
        await _store_flow_status(flow)
    else:
        payload = json.loads(public_raw)
        payload["status"] = "failed" if error else "processing_callback"
        payload["error"] = error
        await get_redis().set(
            _flow_key(flow_id),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=_FLOW_TTL_SECONDS,
        )


async def cancel_flow(flow_id: str, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    """Idempotently stop an OAuth flow owned by the current installer."""
    status = await get_flow_status(flow_id, owner_user_id=owner_user_id)
    if status["status"] in {"completed", "failed"}:
        return status
    await complete_callback(flow_id, error="OAuth 登录已取消")
    return await get_flow_status(flow_id, owner_user_id=owner_user_id)


async def get_flow_status(flow_id: str, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    raw = await get_redis().get(_flow_key(flow_id))
    if not raw:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    payload = json.loads(raw)
    if payload.get("owner_user_id") != owner_user_id:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    return {
        "flow_id": flow_id,
        "status": str(payload.get("status") or "starting"),
        "error": str(payload.get("error") or ""),
        "result": dict(payload.get("result") or {}),
    }


__all__ = [
    "OAuthBundleStorage",
    "build_oauth_provider",
    "cancel_flow",
    "complete_callback",
    "get_flow_status",
    "public_oauth_callback_url",
    "start_oauth_install",
]
