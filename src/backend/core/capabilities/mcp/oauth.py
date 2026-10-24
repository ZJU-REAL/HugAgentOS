"""Browser OAuth install flow for remote MCP servers."""
from __future__ import annotations
from core.capabilities.mcp.management import materialize_mcp_http_connection as market_dependency_materialize_mcp_http_connection
import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse
from core.config.settings import settings
from core.db.models import AdminMcpServer
from core.harness.registry.marketplace_catalog import marketplace_catalog
from core.infra.crypto import decrypt_secret, encrypt_secret
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.ephemeral import get_ephemeral_state
from core.capabilities.mcp.management import probe_mcp_connectivity, validate_remote_mcp_url
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata
from sqlalchemy.orm import Session
from core.capabilities.mcp import oauth_storage


FLOW_TTL_SECONDS = 10 * 60


DISCOVERY_GRACE_SECONDS = 5.0


@dataclass
class OAuthInstallFlow:
    flow_id: str
    slug: str
    owner_user_id: Optional[str]
    installed_by: Optional[str]
    auth_method: str
    credentials: Dict[str, str]
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
    callback_issuer: Optional[str] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    callback_received: asyncio.Event = field(default_factory=asyncio.Event)


def flow_key(flow_id: str) -> str:
    return f"mcp:oauth:flow:{flow_id}"


def callback_key(flow_id: str) -> str:
    return f"mcp:oauth:callback:{flow_id}"


async def store_flow_status(flow: OAuthInstallFlow) -> None:
    payload = {
        "flow_id": flow.flow_id,
        "slug": flow.slug,
        "owner_user_id": flow.owner_user_id,
        "status": flow.status,
        "authorization_url": flow.authorization_url,
        "error": flow.error,
        "result": dict(flow.result or {}),
    }
    await get_ephemeral_state().put(
        flow_key(flow.flow_id),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ttl=FLOW_TTL_SECONDS,
    )


def public_oauth_callback_url(request: Any) -> str:
    """Build a trusted browser callback without reflecting an arbitrary Host header."""
    configured = settings.server.mcp_oauth_public_base_url
    if configured:
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
        return f"{configured}/v1/mcp-market/oauth/callback"

    origin = str(request.headers.get("origin") or "").rstrip("/")
    parsed_origin = urlparse(origin)
    if not origin or parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        raise BadRequestError(message="OAuth 登录必须从浏览器发起")
    request_host = str(request.headers.get("host") or request.url.netloc).lower()
    if settings.server.is_prod and parsed_origin.netloc.lower() != request_host:
        raise BadRequestError(
            message="跨域部署必须配置 MCP_OAUTH_PUBLIC_BASE_URL（例如 http(s)://app.example.com/api）"
        )
    return f"{origin}/api/v1/mcp-market/oauth/callback"


def purge_expired_flows() -> None:
    cutoff = time.time() - FLOW_TTL_SECONDS
    for flow_id, flow in list(process_state().flows.items()):
        if flow.created_at < cutoff:
            process_state().flows.pop(flow_id, None)


def get_version(db: Session, slug: str) -> tuple[Any, Any]:
    return marketplace_catalog().mcp_version(db, slug)


def _oauth_method(db: Session, slug: str, auth_method: str):
    _, version = get_version(db, slug)
    auth_config = marketplace_catalog().normalize_auth_config(
        version.auth_config, list(version.auth_schema or [])
    )
    return next((row for row in auth_config["methods"] if row["id"] == auth_method), None)


async def start_oauth_install(
    *,
    slug: str,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    auth_method: str,
    credentials: Dict[str, str],
    callback_url_base: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, str]:
    """Start a browser OAuth install and wait only until the authorize URL is known."""


    from core.db.threads import run_in_session

    purge_expired_flows()
    method = await run_in_session(_oauth_method, slug, auth_method)
    if not method or method["type"] != "oauth2":
        raise BadRequestError(message="该 MCP 不支持所选 OAuth 认证方式")
    if method.get("client_id_required") and not client_id.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client ID")
    if method.get("client_secret_required") and not client_secret.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client Secret")
    flow_id = f"mcpoauth_{uuid.uuid4().hex}"
    callback_url = f"{callback_url_base}?{urlencode({'flow_id': flow_id})}"
    flow = OAuthInstallFlow(
        flow_id=flow_id,
        slug=slug,
        owner_user_id=owner_user_id,
        installed_by=installed_by,
        auth_method=auth_method,
        credentials=dict(credentials or {}),
        callback_url=callback_url,
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
    )
    process_state().flows[flow_id] = flow
    await store_flow_status(flow)
    asyncio.create_task(run_flow(flow), name=f"mcp-oauth-{flow_id}")
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(flow.ready.wait(), timeout=DISCOVERY_GRACE_SECONDS)
    if flow.status == "failed":
        raise BadRequestError(message=flow.error or "无法启动 OAuth 登录")
    return {
        "flow_id": flow.flow_id,
        "authorization_url": flow.authorization_url,
        "status": flow.status,
    }


def _flow_inputs(db, flow: "OAuthInstallFlow"):
    """What the OAuth probe needs from the market listing, read in one session."""
    item, version = get_version(db, flow.slug)
    auth_config = marketplace_catalog().normalize_auth_config(
        version.auth_config, list(version.auth_schema or [])
    )
    method = next(row for row in auth_config["methods"] if row["id"] == flow.auth_method)
    headers = marketplace_catalog().installation_secrets(
        version,
        flow.credentials,
        {},
        auth_method=flow.auth_method,
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
        is_enabled=False,
        extra_config={},
    )
    runtime_url, _ = market_dependency_materialize_mcp_http_connection(version.url, headers)
    return method, candidate, runtime_url


async def run_flow(flow: OAuthInstallFlow) -> None:
    """Drive one OAuth install; it may wait minutes for the user and holds no session."""
    from core.db.threads import run_in_session

    try:
        method, candidate, runtime_url = await run_in_session(_flow_inputs, flow)
        await validate_remote_mcp_url(runtime_url, require_https=False)

        metadata = OAuthClientMetadata(
            application_type=("native" if urlparse(flow.callback_url).hostname in {"localhost", "127.0.0.1", "::1"} else "web"),
            redirect_uris=[flow.callback_url],
            token_endpoint_auth_method=("client_secret_post" if flow.client_secret else "none"),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(method.get("scopes") or []) or None,
            client_name="HugAgentOS MCP Client",
        )
        storage = oauth_storage.OAuthBundleStorage(
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
            await store_flow_status(flow)
            flow.ready.set()

        async def callback_handler() -> AuthorizationCodeResult:
            deadline = time.monotonic() + 300.0
            while time.monotonic() < deadline:
                if flow.callback_received.is_set():
                    if flow.error:
                        raise RuntimeError(flow.error)
                    return AuthorizationCodeResult(code=flow.callback_code or "", state=flow.callback_state, iss=flow.callback_issuer)
                raw = await get_ephemeral_state().get(callback_key(flow.flow_id))
                if raw:
                    payload = json.loads(raw)
                    callback_error = str(payload.get("error") or "")
                    if callback_error:
                        raise RuntimeError(callback_error)
                    encrypted_code = str(payload.get("code") or "")
                    callback_code = decrypt_secret(encrypted_code) if encrypted_code else ""
                    return AuthorizationCodeResult(code=callback_code or "", state=payload.get("state"), iss=payload.get("iss"))
                await asyncio.sleep(0.5)
            raise TimeoutError("OAuth 登录回调超时")

        provider = oauth_storage.build_oauth_provider(
            runtime_url,
            storage.bundle,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        ok, error = await probe_mcp_connectivity(
            candidate,
            validate=True,
            oauth_provider=provider,
            timeout_seconds=300.0,
        )
        if not ok:
            raise RuntimeError(f"OAuth MCP 连接失败：{error}")
        if flow.error:
            raise RuntimeError(flow.error)
        persisted = await get_ephemeral_state().get(flow_key(flow.flow_id))
        if persisted:
            persisted_status = json.loads(persisted)
            if persisted_status.get("status") == "failed":
                raise RuntimeError(str(persisted_status.get("error") or "OAuth 登录已取消"))
        bundle = storage.capture_context(provider)
        flow.status = "installing"
        await store_flow_status(flow)
        flow.result = await marketplace_catalog().install_market_item(
            flow.slug,
            owner_user_id=flow.owner_user_id,
            installed_by=flow.installed_by,
            credentials=flow.credentials,
            auth_method=flow.auth_method,
            oauth_bundle=bundle,
        )
        flow.status = "completed"
        await store_flow_status(flow)
    except BaseException as exc:  # noqa: BLE001
        flow.status = "failed"
        flow.error = str(exc)
        await store_flow_status(flow)
    finally:
        flow.ready.set()


async def complete_callback(
    flow_id: str,
    *,
    code: str = "",
    state: Optional[str] = None,
    iss: Optional[str] = None,
    error: str = "",
) -> None:
    flow = process_state().flows.get(flow_id)
    public_raw = await get_ephemeral_state().get(flow_key(flow_id))
    if not flow and not public_raw:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    await get_ephemeral_state().put(
        callback_key(flow_id),
        json.dumps(
            {
                "code": encrypt_secret(code) if code else "",
                "state": state,
                "iss": iss,
                "error": error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        ttl=FLOW_TTL_SECONDS,
    )
    if flow:
        flow.callback_code = code
        flow.callback_state = state
        flow.callback_issuer = iss
        flow.error = error
        flow.status = "failed" if error else "processing_callback"
        flow.callback_received.set()
        await store_flow_status(flow)
    else:
        payload = json.loads(public_raw)
        payload["status"] = "failed" if error else "processing_callback"
        payload["error"] = error
        await get_ephemeral_state().put(
            flow_key(flow_id),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ttl=FLOW_TTL_SECONDS,
        )


async def cancel_flow(flow_id: str, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    """Idempotently stop an OAuth flow owned by the current installer."""
    status = await get_flow_status(flow_id, owner_user_id=owner_user_id)
    if status["status"] in {"completed", "failed"}:
        return status
    await complete_callback(flow_id, error="OAuth 登录已取消")
    return await get_flow_status(flow_id, owner_user_id=owner_user_id)


async def get_flow_status(flow_id: str, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    raw = await get_ephemeral_state().get(flow_key(flow_id))
    if not raw:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    payload = json.loads(raw)
    if payload.get("owner_user_id") != owner_user_id:
        raise ResourceNotFoundError("mcp_oauth_flow", flow_id)
    return {
        "flow_id": flow_id,
        "status": str(payload.get("status") or "starting"),
        "authorization_url": str(payload.get("authorization_url") or ""),
        "error": str(payload.get("error") or ""),
        "result": dict(payload.get("result") or {}),
    }


@dataclass
class OauthState:
    flows: Dict[str, OAuthInstallFlow] = field(default_factory=lambda: {})


_STATE = OauthState()


def process_state() -> OauthState:
    return _STATE
