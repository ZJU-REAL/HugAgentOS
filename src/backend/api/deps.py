"""Shared authentication dependencies."""

import os
from typing import Optional

from core.auth.backend import UserContext, require_auth
from core.config.settings import settings
from core.db.models import UserShadow
from core.db.repository import AuditLogRepository
from core.db.threads import run_in_session
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session


def _require_token_or_flag(
    env_var: str, settings_attr: str, label: str, action_base: str, meta_flag: str
):
    """Factory for admin/config auth dependencies.

    Either path grants access:
    1. Static token: ``Authorization: Bearer <ADMIN_TOKEN/CONFIG_TOKEN>`` matches (original behavior);
    2. Session user: the current logged-in user's ``metadata`` carries ``meta_flag``
       (or ``role==super_admin``) — this lets an admin grant console access per
       user on the user-management page, and grantees reach it via session cookie
       without a token.

    When neither holds, keep the original logic: write an audit denial only if a
    header was sent (avoids amplifying probe traffic), then raise 401.

    ``action_base`` (e.g. ``"admin"`` / ``"config"``) derives the paired audit
    actions ``{base}.access_granted`` / ``{base}.access_denied``, so the two
    action names are not string-derived from each other (fragile coupling).

    Like every dependency here, it resolves the session (Redis) on the event
    loop and does all of its database work in one ``run_in_session`` call.
    """
    granted_action = f"{action_base}.access_granted"
    denied_action = f"{action_base}.access_denied"

    def authorize(
        db: Session, request: Request, authorization: Optional[str], user_id: Optional[str]
    ) -> None:
        token = os.getenv(env_var) or getattr(settings.auth, settings_attr, "")
        # ① Static token match
        if token and authorization == f"Bearer {token}":
            _audit_grant(db, request, granted_action, user_id=None, via="token")
            return
        # ② Session user carries the corresponding capability flag (or super_admin)
        if user_id and _user_has_meta_flag(db, user_id, meta_flag):
            _audit_grant(db, request, granted_action, user_id=user_id, via="session")
            return
        # ②b Host identity (desktop bridge): the shell-injected cloud identity is
        # accepted when it carries the same flag. Deployments without a host
        # resolver, or requests it does not claim, skip this without any I/O.
        from core.harness.registry.authentication import host_user_resolver

        resolver = host_user_resolver(request)
        host_user = resolver(request, db) if resolver is not None else None
        if host_user is not None and _user_has_meta_flag(db, host_user.user_id, meta_flag):
            _audit_grant(
                db, request, granted_action, user_id=host_user.user_id, via="desktop_bridge"
            )
            return
        # ③ Token not configured at all and no session grant → report explicitly that it is not configured
        if not token and not user_id:
            raise HTTPException(
                status_code=503,
                detail=f"{label} access not configured ({env_var} not set)",
            )
        # ④ Deny: only log to DB when a header was sent (suspected attack); bare probe traffic is not logged
        if authorization:
            AuditLogRepository(db).log_denial(
                user_id=user_id,
                action=denied_action,
                reason="invalid_token",
                required=f"{label}_token",
                actual="mismatch",
                resource_type="token",
                request=request,
            )
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def dependency(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> None:
        token = os.getenv(env_var) or getattr(settings.auth, settings_attr, "")
        user_id = None
        if not (token and authorization == f"Bearer {token}"):
            user_id = await _resolve_session_user_id(request)
        await run_in_session(authorize, request, authorization, user_id)

    return dependency


_AUDIT_GRANT_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _audit_grant(
    db: Session, request: Request, granted_action: str, *, user_id: Optional[str], via: str
) -> None:
    """Audit **successful use** of a console key (P0 instrumentation: answers "who did what with ADMIN/CONFIG privileges").

    Only write operations (POST/PUT/PATCH/DELETE) are recorded — GET polling /
    list refreshes are high-volume and low-risk, and recording them all would
    flood audit_logs with noise. ``granted_action`` is passed in explicitly by
    the caller. Best-effort; never blocks the request.
    """
    try:
        if request.method.upper() not in _AUDIT_GRANT_METHODS:
            return
        from core.infra.logging import trace_id_var

        AuditLogRepository(db).create(
            {
                "trace_id": trace_id_var.get() or None,
                "user_id": user_id,
                "action": granted_action,
                "resource_type": "backend_console",
                "details": {
                    "method": request.method.upper(),
                    "path": request.url.path,
                    "via": via,
                },
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "status": "success",
            }
        )
    except Exception:  # noqa: BLE001 — an audit failure must never block a console request
        pass


require_admin = _require_token_or_flag(
    "ADMIN_TOKEN", "admin_token", "Admin", "admin", "can_content_manage"
)
require_config = _require_token_or_flag(
    "CONFIG_TOKEN", "config_token", "Config", "config", "can_system_config"
)


async def require_admin_or_config(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """Accept either ADMIN_TOKEN or CONFIG_TOKEN.

    Used by the prompt snapshot import/export endpoints: the CLI migration
    script sends ADMIN_TOKEN, while the Config console "prompt management" page
    sends CONFIG_TOKEN — both should be allowed through.
    """
    admin_token = os.getenv("ADMIN_TOKEN") or getattr(settings.auth, "admin_token", "")
    config_token = os.getenv("CONFIG_TOKEN") or getattr(settings.auth, "config_token", "")
    if not admin_token and not config_token:
        raise HTTPException(
            status_code=503,
            detail="Admin/Config access not configured (ADMIN_TOKEN / CONFIG_TOKEN not set)",
        )
    if authorization:
        if admin_token and authorization == f"Bearer {admin_token}":
            return
        if config_token and authorization == f"Bearer {config_token}":
            return
        await run_in_session(
            lambda db: AuditLogRepository(db).log_denial(
                user_id=None,
                action="prompt_snapshot.access_denied",
                reason="invalid_token",
                required="admin_or_config_token",
                actual="mismatch",
                resource_type="token",
                request=request,
            )
        )
    raise HTTPException(status_code=401, detail="Unauthorized")


def _resolve_current_user_id(request: Request) -> Optional[str]:
    """Recover user_id from the request: prefer state.user (middleware-injected), otherwise fall back to the session cookie."""
    user = getattr(request.state, "user", None)
    if user:
        return user.get("user_id") if isinstance(user, dict) else getattr(user, "user_id", None)
    return None


async def _resolve_session_user_id(request: Request) -> Optional[str]:
    """Asynchronously recover the session user_id: check middleware-injected state.user first, otherwise validate the session cookie.

    No middleware currently writes ``state.user``, so we must actually validate
    the session cookie here (same source as ``/v1/auth/session/check``) so that
    session-based console authorization can match a logged-in user.
    """
    uid = _resolve_current_user_id(request)
    if uid:
        return uid
    try:
        from core.auth.session import validate_session

        token = request.cookies.get(settings.session.cookie_name)
        if token:
            payload = await validate_session(token)
            if payload:
                return payload.get("user_id")
    except Exception:  # noqa: BLE001 — auth failure degrades safely to anonymous
        return None
    return None


async def console_actor(request: Request) -> str:
    """Who is acting on a console endpoint: the session user, else the static token.

    A dependency, so the console routes that record it can stay plain ``def``
    routes and do their database work in FastAPI's worker threads.
    """
    return await _resolve_session_user_id(request) or "config_token"


def _is_super_admin(db: Session, user_id: str) -> bool:
    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    return bool(shadow and (shadow.extra_data or {}).get("role") == "super_admin")


def _user_has_meta_flag(db: Session, user_id: str, flag: str) -> bool:
    """Whether the session user holds a page-level console capability flag (personal → role → team default → system default).

    Thin wrapper over ``capabilities.user_has_capability`` (single implementation,
    shared with the security plugin) — super_admin implies everything;
    role-granted can_system_config / can_content_manage also open the console
    through this path.
    """
    from core.auth.capabilities import user_has_capability

    return user_has_capability(db, user_id, flag)


def user_can_manage_system_settings(db: Session, user_id: Optional[str]) -> bool:
    """Whether the current user may manage "personal system settings" (delegated endpoints such as model access / service config).

    Two grant paths (the token fallback is handled by ``require_system_settings``
    itself, not here):

    1. Capability flag ``can_system_config`` (implied by ``super_admin``) —
       covers the admin created by ``hugagent onboard`` in local single-node
       mode and EE-authorized users;
    2. CE + mock single trust domain: when ``JX_EDITION=ce`` and
       ``AUTH_MODE=mock``, allow any authenticated user — in mock mode every
       request resolves to the same default user, there is no identity boundary
       to begin with, and locking to super_admin would only lock the sole user out.
    """
    if not user_id:
        return False
    if _user_has_meta_flag(db, user_id, "can_system_config"):
        return True
    return settings.edition.edition == "ce" and settings.auth.mode == "mock"


def user_can_manage_ontology_governance(db: Session, user_id: Optional[str]) -> bool:
    """Whether a signed-in CE instance owner may manage global ontology assets.

    The management surface is intentionally delegated only in CE, where the
    separate Admin console is not shipped.  A CE user must still be either a
    content/system administrator (super-admin implies both) or the sole mock
    user in the single-trust-domain development mode.
    """
    if not user_id or settings.edition.edition != "ce":
        return False
    return _user_has_meta_flag(
        db, user_id, "can_content_manage"
    ) or user_can_manage_system_settings(db, user_id)


def _authorize_delegated(
    db: Session,
    request: Request,
    user: Optional[UserContext],
    *,
    token: str,
    token_grant: str,
    allowed,
    action: str,
    required: str,
    forbidden: str,
) -> str:
    """Shared body of the delegated console gates (runs in a worker thread).

    A matching static token wins; otherwise the resolved user must pass
    ``allowed(db, user_id)``. Grants are audited for writes, denials whenever a
    credential or user was presented.
    """
    authorization = request.headers.get("authorization")
    if token and authorization == f"Bearer {token}":
        _audit_grant(db, request, f"{action}.access_granted", user_id=None, via="token")
        return token_grant
    if user is not None and allowed(db, user.user_id):
        _audit_grant(db, request, f"{action}.access_granted", user_id=user.user_id, via="session")
        return user.user_id
    if authorization or user is not None:
        AuditLogRepository(db).log_denial(
            user_id=user.user_id if user else None,
            action=f"{action}.access_denied",
            reason="insufficient_permission",
            required=required,
            actual="regular_user" if user else "anonymous",
            resource_type="backend_console",
            request=request,
        )
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    raise HTTPException(status_code=403, detail=forbidden)


async def require_system_settings(
    request: Request,
    user: Optional[UserContext] = Depends(require_auth(False)),
) -> str:
    """System-settings seam (the CE delegated version of ``require_config``): any of three paths grants access.

    1. Static CONFIG_TOKEN (script / ops fallback, equivalent to ``require_config``);
    2. Session/mock user with the ``can_system_config`` capability flag (implied by super_admin);
    3. CE + mock single trust domain (see ``user_can_manage_system_settings``).

    Key difference from ``require_config``: the user is resolved via the full
    ``get_current_user`` chain — the CE derived tree has no
    ``core/auth/session.py``, so ``require_config``'s cookie-validation path
    always fails on CE; this gate can resolve a user in mock / local-session /
    API-Key modes alike.
    """
    return await run_in_session(
        _authorize_delegated,
        request,
        user,
        token=os.getenv("CONFIG_TOKEN") or getattr(settings.auth, "config_token", ""),
        token_grant="config_token",
        allowed=user_can_manage_system_settings,
        action="system_settings",
        required="can_system_config",
        forbidden="需要系统配置权限",
    )


async def require_ontology_governance(
    request: Request,
    user: Optional[UserContext] = Depends(require_auth(False)),
) -> str:
    """Authorize the CE settings-page ontology governance surface.

    ``ADMIN_TOKEN`` remains an operational fallback. Browser access is limited
    to users accepted by :func:`user_can_manage_ontology_governance`.
    """
    return await run_in_session(
        _authorize_delegated,
        request,
        user,
        token=os.getenv("ADMIN_TOKEN") or getattr(settings.auth, "admin_token", ""),
        token_grant="admin_token",
        allowed=user_can_manage_ontology_governance,
        action="ontology_governance",
        required="ce_ontology_governance",
        forbidden="需要 CE 本体治理权限",
    )


def _authorize_super_admin(db: Session, request: Request) -> str:
    user_id = _resolve_current_user_id(request)
    if not user_id:
        AuditLogRepository(db).log_denial(
            user_id=None,
            action="super_admin.access_denied",
            reason="not_logged_in",
            required="super_admin",
            actual="anonymous",
            request=request,
        )
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_super_admin(db, user_id):
        AuditLogRepository(db).log_denial(
            user_id=user_id,
            action="super_admin.access_denied",
            reason="not_super_admin",
            required="super_admin",
            actual="regular_user",
            request=request,
        )
        raise HTTPException(status_code=403, detail="需要 super_admin 权限")
    return user_id


async def require_super_admin(request: Request) -> str:
    """Require the current session user to be super_admin; a valid ADMIN_TOKEN serves as fallback."""
    admin_token = os.getenv("ADMIN_TOKEN") or settings.auth.admin_token
    authorization = request.headers.get("authorization")
    if admin_token and authorization == f"Bearer {admin_token}":
        return "admin_token"
    return await run_in_session(_authorize_super_admin, request)
