"""Encrypted token storage that backs the MCP SDK's OAuth provider."""
from __future__ import annotations
import json
import time
from typing import Any, Dict, Optional
from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer
from core.capabilities.mcp.management import decrypt_mcp_headers, encrypt_mcp_headers, mcp_oauth_bundle_storage_key, validate_remote_mcp_url
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken, ProtectedResourceMetadata

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
            time.time() + int(tokens.expires_in) if tokens.expires_in is not None else None
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
            row = (
                db.query(AdminMcpServer).filter(AdminMcpServer.server_id == self.server_id).first()
            )
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
    client_metadata_url: Optional[str] = None,
    allow_private_network: Optional[bool] = None,
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
        client_metadata_url=client_metadata_url,
    )
    private_ok = (
        bool(bundle.get("allow_private_network"))
        if allow_private_network is None
        else allow_private_network
    )

    async def _validate_oauth_request(request) -> None:
        # OAuth discovery metadata is controlled by the remote resource.  Apply
        # the same DNS/IP SSRF boundary to every SDK-generated request, not only
        # to the original MCP URL.
        # Intranet MCPs whose install was admitted to a private network keep
        # that allowance; link-local and metadata ranges stay blocked regardless.
        await validate_remote_mcp_url(
            str(request.url), allow_private_network=private_ok, require_https=False
        )

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
