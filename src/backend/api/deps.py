"""Shared authentication dependencies."""

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user, require_auth
from core.auth.roles import TeamRole, at_least
from core.auth.permissions_iface import (
    PermissionLevel,
    require_team_file_permission,
)
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import UserShadow
from core.db.repository import AuditLogRepository, TeamRepository


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
    """
    granted_action = f"{action_base}.access_granted"
    denied_action = f"{action_base}.access_denied"

    async def dependency(
        request: Request,
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
    ) -> None:
        token = os.getenv(env_var) or getattr(settings.auth, settings_attr, "")
        # ① Static token match
        if token and authorization == f"Bearer {token}":
            _audit_grant(db, request, granted_action, user_id=None, via="token")
            return
        # ② Session user carries the corresponding capability flag (or super_admin)
        user_id = await _resolve_session_user_id(request)
        if user_id and _user_has_meta_flag(db, user_id, meta_flag):
            _audit_grant(db, request, granted_action, user_id=user_id, via="session")
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

        AuditLogRepository(db).create({
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
        })
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
    db: Session = Depends(get_db),
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
        AuditLogRepository(db).log_denial(
            user_id=None,
            action="prompt_snapshot.access_denied",
            reason="invalid_token",
            required="admin_or_config_token",
            actual="mismatch",
            resource_type="token",
            request=request,
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
    except Exception:  # noqa: BLE001 — missing cookie / expired session degrades safely to anonymous
        return None
    return None


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


async def require_system_settings(
    request: Request,
    db: Session = Depends(get_db),
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
    token = os.getenv("CONFIG_TOKEN") or getattr(settings.auth, "config_token", "")
    authorization = request.headers.get("authorization")
    if token and authorization == f"Bearer {token}":
        _audit_grant(db, request, "system_settings.access_granted", user_id=None, via="token")
        return "config_token"
    if user is not None and user_can_manage_system_settings(db, user.user_id):
        _audit_grant(
            db, request, "system_settings.access_granted", user_id=user.user_id, via="session"
        )
        return user.user_id
    if authorization or user is not None:
        AuditLogRepository(db).log_denial(
            user_id=user.user_id if user else None,
            action="system_settings.access_denied",
            reason="insufficient_permission",
            required="can_system_config",
            actual="regular_user" if user else "anonymous",
            resource_type="backend_console",
            request=request,
        )
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    raise HTTPException(status_code=403, detail="需要系统配置权限")


async def require_super_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> str:
    """Require the current session user to be super_admin; a valid ADMIN_TOKEN serves as fallback."""
    admin_token = os.getenv("ADMIN_TOKEN") or settings.auth.admin_token
    authorization = request.headers.get("authorization")
    if admin_token and authorization == f"Bearer {admin_token}":
        return "admin_token"

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


def require_team_role(min_role: TeamRole = "member"):
    """FastAPI dependency factory: require the current user to have >= min_role in the team given by the team_id path parameter."""

    async def dependency(
        team_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> str:
        user_id = _resolve_current_user_id(request)
        if not user_id:
            AuditLogRepository(db).log_denial(
                user_id=None,
                action="team_role.access_denied",
                reason="not_logged_in",
                required=min_role,
                actual="anonymous",
                resource_type="team",
                resource_id=team_id,
                request=request,
            )
            raise HTTPException(status_code=401, detail="未登录")
        if _is_super_admin(db, user_id):
            return user_id
        role = TeamRepository(db).get_member_role(team_id, user_id)
        if role is None:
            AuditLogRepository(db).log_denial(
                user_id=user_id,
                action="team_role.access_denied",
                reason="not_a_member",
                required=min_role,
                actual="none",
                resource_type="team",
                resource_id=team_id,
                request=request,
            )
            raise HTTPException(status_code=403, detail="未加入该团队")
        if not at_least(role, min_role):
            AuditLogRepository(db).log_denial(
                user_id=user_id,
                action="team_role.access_denied",
                reason="insufficient_role",
                required=min_role,
                actual=role,
                resource_type="team",
                resource_id=team_id,
                request=request,
            )
            raise HTTPException(status_code=403, detail=f"需要 {min_role} 以上角色")
        return user_id

    return dependency


# ═══════════════════════════════════════════════════════════════════════
# Team file permissions — unified dependency
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class TeamFileAccess:
    """Access context produced by `require_team_file_perm()`.

    A route signature only needs a single
    ``access: TeamFileAccess = Depends(require_team_file_perm("edit"))`` to get:
    the resolved user / db / the validated permission level.
    """
    user: UserContext
    db: Session
    team_id: str
    permission: PermissionLevel  # the current user's actual permission (view/edit/admin)


def require_team_file_perm(min_permission: PermissionLevel, *, action: str = "team_file.access"):
    """FastAPI dependency factory: verify the current user's file permission on the path ``team_id`` is >= ``min_permission``.

    When not satisfied, ``require_team_file_permission()`` is responsible for
    raising 403/404 and writing the audit denial log.
    """

    async def dependency(
        team_id: str,
        request: Request,
        user: UserContext = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> TeamFileAccess:
        perm = require_team_file_permission(
            db,
            str(user.user_id),
            team_id,
            min_permission,
            request=request,
            action=action,
        )
        return TeamFileAccess(user=user, db=db, team_id=team_id, permission=perm)

    return dependency
