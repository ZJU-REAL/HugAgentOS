"""Administrator-registered OAuth Apps for MCP marketplace listings.

An OAuth listing picks how its OAuth client is obtained:

- ``auto``: the SDK uses a client metadata document (CIMD) when the
  authorization server supports it and this deployment has a public HTTPS
  base, otherwise dynamic client registration (DCR);
- ``dcr``: always DCR, for servers that advertise CIMD but cannot reach us;
- ``manual``: each installer brings their own OAuth App;
- ``admin_app``: one administrator-registered OAuth App for everyone.

``admin_app`` is not stored on the listing: it is the presence of a row in
``mcp_market_oauth_clients``.  Curated templates are re-seeded from code on
startup, so keeping the choice outside ``auth_config`` lets it survive that.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from core.infra.crypto import decrypt_secret, encrypt_secret
from core.infra.exceptions import BadRequestError
from core.infra.time import utc_now
from features.marketplace.models import McpMarketOAuthClient
from sqlalchemy.orm import Session

REGISTRATION_MODES = ("auto", "dcr", "manual", "admin_app")
# Values written by earlier releases; both meant "register dynamically if possible".
_LEGACY_REGISTRATION = {"dynamic": "auto", "dynamic_or_manual": "auto"}


def normalize_registration(value: Any) -> str:
    raw = str(value or "auto").strip()
    raw = _LEGACY_REGISTRATION.get(raw, raw)
    # admin_app is derived from the client table, never persisted on a method.
    return raw if raw in {"auto", "dcr", "manual"} else "auto"


def admin_client(db: Session, slug: str) -> Optional[Tuple[str, str]]:
    """The listing's administrator OAuth App as ``(client_id, client_secret)``."""
    row = db.query(McpMarketOAuthClient).filter(McpMarketOAuthClient.slug == slug).first()
    if row is None or not row.client_id:
        return None
    secret = decrypt_secret(row.client_secret) if row.client_secret else ""
    return row.client_id, secret or ""


def has_admin_client(db: Session, slug: str) -> bool:
    return (
        db.query(McpMarketOAuthClient.slug).filter(McpMarketOAuthClient.slug == slug).first()
        is not None
    )


def save_admin_client(
    db: Session,
    slug: str,
    *,
    client_id: str,
    client_secret: str = "",
    updated_by: Optional[str] = None,
) -> None:
    """Upsert the OAuth App; an empty secret keeps the stored one (public clients have none)."""
    normalized_id = (client_id or "").strip()
    if not normalized_id:
        raise BadRequestError(message="请填写管理员 OAuth App 的 Client ID")
    row = db.query(McpMarketOAuthClient).filter(McpMarketOAuthClient.slug == slug).first()
    now = utc_now()
    if row is None:
        row = McpMarketOAuthClient(slug=slug, created_at=now)
        db.add(row)
    elif row.client_id != normalized_id and not (client_secret or "").strip():
        # A secret belongs to one client; never pair an old secret with a new ID.
        row.client_secret = None
    row.client_id = normalized_id
    if (client_secret or "").strip():
        row.client_secret = encrypt_secret(client_secret.strip())
    row.updated_by = updated_by
    row.updated_at = now


def clear_admin_client(db: Session, slug: str) -> None:
    db.query(McpMarketOAuthClient).filter(McpMarketOAuthClient.slug == slug).delete(
        synchronize_session=False
    )


def present_oauth_methods(db: Session, slug: str, auth_config: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate OAuth methods with their effective registration for API consumers.

    With an administrator OAuth App configured, installers are never asked for
    a Client ID or secret, even on templates that would otherwise require one.
    """
    configured = has_admin_client(db, slug)
    methods = []
    for method in auth_config.get("methods") or []:
        if method.get("type") == "oauth2":
            method = dict(method)
            method["client_registration"] = (
                "admin_app" if configured else normalize_registration(method.get("client_registration"))
            )
            method["admin_client_configured"] = configured
            if configured:
                method["client_id_required"] = False
                method["client_secret_required"] = False
        methods.append(method)
    return {**auth_config, "methods": methods}


def apply_oauth_policy(
    db: Session,
    slug: str,
    auth_config: Dict[str, Any],
    *,
    curated: bool,
    registration: Optional[str],
    client_id: Optional[str],
    client_secret: Optional[str],
    scopes: Optional[list],
    updated_by: Optional[str],
) -> Dict[str, Any]:
    """Apply an administrator's OAuth registration choice to a listing.

    Curated templates are re-seeded from code, so for them only the
    administrator OAuth App (kept outside ``auth_config``) is editable.
    """
    if registration is None and scopes is None:
        return auth_config
    if not any(method.get("type") == "oauth2" for method in auth_config.get("methods") or []):
        raise BadRequestError(message="该 MCP 未启用 OAuth 认证")
    if registration is not None and registration not in REGISTRATION_MODES:
        raise BadRequestError(message="OAuth 客户端注册方式无效")
    if registration == "admin_app":
        if (client_id or "").strip() or not has_admin_client(db, slug):
            save_admin_client(
                db,
                slug,
                client_id=client_id or "",
                client_secret=client_secret or "",
                updated_by=updated_by,
            )
        elif (client_secret or "").strip():
            row = db.query(McpMarketOAuthClient).filter(McpMarketOAuthClient.slug == slug).first()
            row.client_secret = encrypt_secret(client_secret.strip())
            row.updated_by = updated_by
            row.updated_at = utc_now()
    elif registration is not None:
        clear_admin_client(db, slug)
    if curated:
        return auth_config
    methods = []
    for method in auth_config.get("methods") or []:
        if method.get("type") == "oauth2":
            method = dict(method)
            if registration is not None and registration != "admin_app":
                method["client_registration"] = registration
                method["client_id_required"] = registration == "manual"
                method["client_secret_required"] = False
            if scopes is not None:
                method["scopes"] = [str(scope).strip() for scope in scopes if str(scope).strip()]
        methods.append(method)
    return {**auth_config, "methods": methods}


def submission_auth_config(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """OAuth contract for listing a user's OAuth-connected MCP, never its credentials.

    Installers each authorize on their own.  When the submitter registered
    automatically, installers will too; when they brought their own OAuth App,
    installers must as well until an administrator configures a shared one.
    """
    manual = bundle.get("client_registration") == "manual"
    scope = str((bundle.get("client_metadata") or {}).get("scope") or "")
    method: Dict[str, Any] = {
        "id": "oauth2",
        "type": "oauth2",
        "label": "OAuth 登录",
        "client_registration": "manual" if manual else "auto",
        "scopes": [part for part in scope.split() if part],
    }
    if manual:
        method["client_id_required"] = True
        method["client_secret_required"] = bool(bundle.get("client_secret_supplied"))
    return {"default_method": "oauth2", "methods": [method], "credential_mode": "installer"}
