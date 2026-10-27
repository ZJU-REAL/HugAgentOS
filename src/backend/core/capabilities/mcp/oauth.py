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
from urllib.parse import parse_qs, urlparse
from core.db.models import AdminMcpServer
from core.harness.registry.marketplace_catalog import marketplace_catalog
from core.infra.crypto import decrypt_secret, encrypt_secret
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.ephemeral import get_ephemeral_state
from core.capabilities.mcp.management import mcp_oauth_bundle_storage_key, probe_mcp_connectivity, validate_remote_mcp_url
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata
from sqlalchemy.orm import Session
from core.capabilities.mcp import oauth_client, oauth_storage, private_servers
from core.config.settings import settings


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
    # Set for a user's self-service private MCP instead of a marketplace install.
    private_target: Optional[Dict[str, Any]] = None
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


def state_key(state: str) -> str:
    # The redirect URI is fixed per deployment, so the callback finds its flow
    # through the SDK-generated ``state`` instead of a per-flow query string.
    return f"mcp:oauth:state:{state}"


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
    method = next((row for row in auth_config["methods"] if row["id"] == auth_method), None)
    return method, marketplace_catalog().mcp_oauth_admin_client(db, slug) is not None


async def start_oauth_install(
    *,
    slug: str,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    auth_method: str,
    credentials: Dict[str, str],
    api_base: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, str]:
    """Start a browser OAuth install and wait only until the authorize URL is known."""


    from core.db.threads import run_in_session

    purge_expired_flows()
    method, admin_app = await run_in_session(_oauth_method, slug, auth_method)
    if not method or method["type"] != "oauth2":
        raise BadRequestError(message="该 MCP 不支持所选 OAuth 认证方式")
    if admin_app:
        # The administrator's OAuth App is authoritative; never let an
        # installer substitute their own client for a managed listing.
        client_id = client_secret = ""
    if not admin_app and method.get("client_id_required") and not client_id.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client ID")
    if not admin_app and method.get("client_secret_required") and not client_secret.strip():
        raise BadRequestError(message="该 OAuth 服务需要 Client Secret")
    flow_id = f"mcpoauth_{uuid.uuid4().hex}"
    callback_url = oauth_client.callback_url(api_base)
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
    return await _launch(flow)


async def start_private_oauth(
    *,
    owner_user_id: str,
    target: Dict[str, Any],
    api_base: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, str]:
    """Start consent for a user's own OAuth-protected MCP; it is saved once it connects."""
    purge_expired_flows()
    flow = OAuthInstallFlow(
        flow_id=f"mcpoauth_{uuid.uuid4().hex}",
        slug="",
        owner_user_id=owner_user_id,
        installed_by=owner_user_id,
        auth_method="oauth2",
        credentials={},
        callback_url=oauth_client.callback_url(api_base),
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        private_target=dict(target),
    )
    return await _launch(flow)


async def _launch(flow: OAuthInstallFlow) -> Dict[str, str]:
    """Run the flow in the background and hand back once the authorize URL is known."""
    flow_id = flow.flow_id
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
    policy = marketplace_catalog().mcp_oauth_install_policy(db, flow.slug, runtime_url)
    return method, candidate, runtime_url, policy


def _private_inputs(flow: "OAuthInstallFlow"):
    """Inputs for a self-service MCP: the user's own endpoint, no marketplace policy."""
    target = flow.private_target or {}
    headers = dict(target.get("headers") or {})
    candidate = private_servers.build_private_server(
        owner_user_id=str(flow.owner_user_id),
        display_name=str(target.get("display_name") or ""),
        description=str(target.get("description") or ""),
        user_intro=target.get("user_intro"),
        transport=str(target.get("transport") or "streamable_http"),
        url=str(target.get("url") or ""),
        headers=headers,
        icon=target.get("icon"),
    )
    runtime_url, _ = market_dependency_materialize_mcp_http_connection(candidate.url, headers)
    method = {"client_registration": "auto", "scopes": list(target.get("scopes") or [])}
    # A user-supplied endpoint never inherits the admin-published network allowance.
    policy = {
        "admin_client": None,
        "allow_private_network": settings.server.mcp_self_service_allow_private_network,
    }
    return method, candidate, runtime_url, policy


async def run_flow(flow: OAuthInstallFlow) -> None:
    """Drive one OAuth install; it may wait minutes for the user and holds no session."""
    from core.db.threads import run_in_session

    try:
        if flow.private_target is not None:
            method, candidate, runtime_url, policy = _private_inputs(flow)
        else:
            method, candidate, runtime_url, policy = await run_in_session(_flow_inputs, flow)
        allow_private_network = bool(policy["allow_private_network"])
        await validate_remote_mcp_url(
            runtime_url, allow_private_network=allow_private_network, require_https=False
        )
        if policy["admin_client"] is not None:
            flow.client_id, flow.client_secret = policy["admin_client"]
        registration = str(method.get("client_registration") or "auto")
        # Recorded so a later marketplace submission knows whether installers
        # can register automatically or must bring their own OAuth App.
        effective_registration = (
            "admin_app" if policy["admin_client"] is not None
            else "manual" if flow.client_id
            else registration
        )

        metadata = OAuthClientMetadata(
            application_type=("native" if urlparse(flow.callback_url).hostname in {"localhost", "127.0.0.1", "::1"} else "web"),
            redirect_uris=[flow.callback_url],
            token_endpoint_auth_method=("client_secret_post" if flow.client_secret else "none"),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(method.get("scopes") or []) or None,
            client_name=oauth_client.CLIENT_NAME,
        )
        storage = oauth_storage.OAuthBundleStorage(
            {
                "server_url": runtime_url,
                "client_metadata": metadata.model_dump(mode="json", exclude_none=True),
                # Refreshes run later from the stored bundle; they must keep the
                # network boundary this install was admitted under.
                "allow_private_network": allow_private_network,
                "client_registration": effective_registration,
                "client_secret_supplied": bool(flow.client_secret),
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
            state = (parse_qs(urlparse(url).query).get("state") or [""])[0]
            if state:
                await get_ephemeral_state().put(state_key(state), flow.flow_id, ttl=FLOW_TTL_SECONDS)
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
            # Only offered when no client is pinned; the SDK still falls back
            # to DCR when the authorization server does not advertise CIMD.
            client_metadata_url=(
                oauth_client.client_metadata_url()
                if registration == "auto" and not flow.client_id
                else None
            ),
            allow_private_network=allow_private_network,
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
        if flow.private_target is not None:
            headers = dict(flow.private_target.get("headers") or {})
            headers[mcp_oauth_bundle_storage_key()] = json.dumps(
                bundle, ensure_ascii=False, separators=(",", ":")
            )
            flow.result = await private_servers.save_private_server(candidate, headers)
            flow.status = "completed"
            await store_flow_status(flow)
            return
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


async def resolve_callback_flow(flow_id: Optional[str], state: Optional[str]) -> str:
    """Flow for a browser callback: legacy ``flow_id`` query, else the ``state`` mapping."""
    if flow_id:
        return flow_id
    mapped = await get_ephemeral_state().get(state_key(state)) if state else None
    if not mapped:
        raise ResourceNotFoundError("mcp_oauth_flow", state or "")
    return str(mapped)


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
