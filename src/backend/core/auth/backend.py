"""Authentication and authorization middleware.

Supports three AUTH_MODE values:
  - mock:    Development mode, accepts any Bearer token or generates default user
  - remote:  Legacy production mode, verifies Bearer token with AUTH_API_URL/verify
  - session: SSO ticket mode, validates jx_session Cookie against Redis
"""

from typing import Any, Dict, Optional

from core.auth.bearer import MOCK_TOKEN, verify_bearer
from core.auth.edition import AccountCapacityExceeded
from core.config.settings import settings
from core.db.repository import AuditLogRepository
from core.db.threads import run_in_session
from core.infra.logging import get_logger
from core.auth.users import UserService
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

logger = get_logger(__name__)


class UserContext(BaseModel):
    """User context injected into requests after authentication."""

    user_id: str
    user_center_id: str
    username: str
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    api_key_id: Optional[str] = None
    api_key_agent_id: Optional[str] = None
    api_key_project_id: Optional[str] = None
    public_chat_visitor: Optional[str] = None


def _sso_login_url() -> str:
    """Get the SSO login URL for 401 responses (derived automatically by mode)."""
    return settings.sso.effective_login_url or settings.sso.login_url


def _auth_mode() -> str:
    return settings.auth.mode


def _audit_rejected_token(db: Session) -> None:
    # No user to attribute it to: user_id is a foreign key, so it stays NULL, and
    # status must be one of success / failure / error.
    try:
        AuditLogRepository(db).create(
            {
                "user_id": None,
                "action": "auth.login.failed",
                "resource_type": "user",
                "resource_id": "unknown",
                "status": "failure",
                "details": {"reason": "invalid_or_expired_token"},
            }
        )
    except Exception as e:
        logger.warning(f"Failed to log auth failure: {e}")


def _bind_bearer_user(db: Session, identity: Dict[str, Any], *, audit_login: bool) -> UserContext:
    """Map a verified identity onto its shadow user (database work, run off the event loop)."""
    user_shadow = UserService(db).get_or_create_user_shadow(
        user_center_id=identity["user_center_id"],
        username=identity["username"],
        email=identity.get("email"),
        avatar_url=identity.get("avatar_url"),
        refresh_unchanged=False,
    )
    if audit_login:
        AuditLogRepository(db).create(
            {
                "user_id": user_shadow.user_id,
                "action": "auth.login.success",
                "resource_type": "user",
                "resource_id": user_shadow.user_id,
                "status": "success",
            }
        )
    return UserContext(
        user_id=user_shadow.user_id,
        user_center_id=user_shadow.user_center_id,
        username=user_shadow.username,
        email=user_shadow.email,
        avatar_url=user_shadow.avatar_url,
    )


async def _resolve_bearer_user(token: str, *, audit: bool) -> UserContext:
    """Verify a mock / remote Bearer token and bind it to its shadow user.

    A login is audited when the user center actually verified the token, not
    on every request that reuses that verification.
    """
    identity, checked = await verify_bearer(token)
    if identity is None:
        if audit:
            await run_in_session(_audit_rejected_token)
        raise HTTPException(
            status_code=401,
            detail={
                "code": 30002,
                "message": "Invalid or expired token",
                "data": {"login_url": _sso_login_url()},
            },
        )
    return await run_in_session(_bind_bearer_user, identity, audit_login=audit and checked)


# Bearer token scheme (do not auto-reject missing header so mock mode can inject dev user)
security = HTTPBearer(auto_error=False)


def _resolve_api_key_context(token: Optional[str], db: Session) -> Optional[UserContext]:
    """If token is a valid personal API-Key (sk-jx-...), resolve it into a UserContext, otherwise None.

    API-Keys are valid under all AUTH_MODEs (external programmatic calls usually
    have no Cookie). Validation (enabled/not-revoked/not-expired/user permission
    bits) is handled uniformly by ``resolve_api_key_identity``.
    """
    from core.auth.api_keys import resolve_api_key_identity

    identity = resolve_api_key_identity(db, token or "")
    if identity is None:
        return None
    user, key = identity
    return UserContext(
        user_id=user.user_id,
        user_center_id=user.user_center_id or "",
        username=user.username,
        email=user.email,
        avatar_url=user.avatar_url,
        api_key_id=key.id,
        api_key_agent_id=key.agent_id,
        api_key_project_id=key.project_id,
    )


def _require_api_key_context(
    db: Session, token: Optional[str], request: Optional[Request] = None
) -> UserContext:
    """Resolve an API-Key; any failure is a 401. A Bearer that looks like an API-Key
    must be valid — never silently downgrade to anonymous. Shared by get_current_user
    and optional_user, guaranteeing the two paths have identical rejection semantics
    and 401 envelopes."""
    ctx = _resolve_api_key_context(token, db)
    if ctx is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": 30002,
                "message": "Invalid or expired API key",
                "data": {},
            },
        )
    if ctx.api_key_agent_id is not None:
        from core.auth.agent_api_scope import enforce_agent_api_route

        if request is None:
            raise HTTPException(status_code=403, detail="Scoped API key requires route context")
        enforce_agent_api_route(request, db, ctx)
    if ctx.api_key_project_id is not None:
        from core.auth.project_api_scope import enforce_project_api_route

        if request is None:
            raise HTTPException(status_code=403, detail="Scoped API key requires route context")
        enforce_project_api_route(request, db, ctx)
    return ctx


async def _resolve_session_user(request: Request) -> Optional[UserContext]:
    """Try to resolve user from jx_session Cookie via Redis.

    Returns UserContext if valid, None if no cookie present.
    Raises HTTPException(401) if cookie present but session expired.
    """
    cookie_name = settings.session.cookie_name
    token = request.cookies.get(cookie_name)
    if not token:
        return None

    # Seam C5: when the session module is missing (CE tree has no remote session), quietly short-circuit to Bearer/mock
    try:
        from core.auth.session import validate_session
    except ModuleNotFoundError:
        return None
    passive_directory = request.url.path.endswith("/v1/workbench/catalog/events") or (
        request.url.path.endswith("/v1/workbench/catalog") and request.headers.get("if-none-match")
    )
    if passive_directory:
        from core.auth.session import _hash_token, find_session_by_hash

        user_data = await find_session_by_hash(_hash_token(token))
    else:
        user_data = await validate_session(token)
    if user_data:
        return UserContext(
            user_id=user_data["user_id"],
            user_center_id=user_data.get("user_center_id", ""),
            username=user_data.get("username", ""),
            email=user_data.get("email"),
            avatar_url=user_data.get("avatar_url"),
        )

    # Cookie exists but Redis session expired → clear 401, do NOT fallback
    raise HTTPException(
        status_code=401,
        detail={
            "code": 30003,
            "message": "Session expired",
            "data": {"login_url": _sso_login_url()},
        },
    )


async def _resolve_host_user(request: Request) -> Optional[UserContext]:
    """Identity injected by the host (desktop shell), when it claims this request.

    Most requests are not claimed — no resolver is registered, or the request
    carries no host credential — and pay no thread hop for asking.
    """
    from core.harness.registry.authentication import host_user_resolver

    resolver = host_user_resolver(request)
    if resolver is None:
        return None
    return await run_in_session(lambda db: resolver(request, db))


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserContext:
    """Dependency to get current authenticated user.

    Authentication priority:
      1. Explicit API Key → credential scope (never widened by a cookie)
      2. Desktop bridge / cookie session → existing identity flow
      3. Other Bearer token → mock / remote verification where supported

    AUTH_MODE=session still accepts explicit API Keys; other Bearer tokens
    require a session cookie.

    It holds no database session: each lookup runs to completion on a session
    of its own in a worker thread, so nothing stays checked out while the
    request (or a stream it starts) continues. Websockets and re-validation
    call it directly with the request and credentials they have.
    """
    if request.headers.get("x-public-chat") is not None:
        from core.auth.public_chat import authenticate_public_chat

        return await run_in_session(lambda db: authenticate_public_chat(request, db))
    auth_mode = _auth_mode()

    # An explicit API credential must not be widened by an accompanying cookie
    # or desktop identity. Invalid/revoked scoped keys likewise fail closed.
    from core.auth.api_keys import api_key_token_from_header, is_api_key_token

    bearer_token = api_key_token_from_header(request.headers.get("authorization"))
    if bearer_token is None and credentials is not None:
        bearer_token = credentials.credentials.strip()
    if is_api_key_token(bearer_token):
        return await run_in_session(_require_api_key_context, bearer_token, request)

    # ── 0. 桌面桥接（仅桌面壳孵化的本机后端启用；混合架构 P2）──
    # 桌面壳注入的云端身份优先于本机自身的会话体系；未启用/未命中时零开销回落。
    bridge_user = await _resolve_host_user(request)
    if bridge_user is not None:
        return bridge_user

    # ── 1. Try Cookie session first (works in all modes) ──
    session_user = await _resolve_session_user(request)
    if session_user is not None:
        return session_user

    # ── 2. No Cookie session ──
    if auth_mode == "session":
        # Pure session mode: Cookie is mandatory, no Bearer fallback
        raise HTTPException(
            status_code=401,
            detail={
                "code": 30001,
                "message": "Authorization required",
                "data": {"login_url": _sso_login_url()},
            },
        )

    # ── 3. mock / remote: Bearer token path ──
    if credentials is None:
        if auth_mode != "mock":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": 30001,
                    "message": "Authorization header required",
                    "data": {"login_url": _sso_login_url()},
                },
            )
        token = MOCK_TOKEN
    else:
        token = credentials.credentials
    return await _resolve_bearer_user(token, audit=True)


def require_auth(required: bool = True):
    """Optional authentication dependency.

    Usage:
        @app.get("/public")
        def public_endpoint(user: Optional[UserContext] = Depends(require_auth(False))):
            pass

        @app.get("/private")
        def private_endpoint(user: UserContext = Depends(require_auth(True))):
            pass
    """
    if required:
        return get_current_user
    else:

        async def optional_user(request: Request) -> Optional[UserContext]:
            if request.headers.get("x-public-chat") is not None:
                from core.auth.public_chat import authenticate_public_chat

                return await run_in_session(lambda db: authenticate_public_chat(request, db))
            auth_mode = _auth_mode()
            from core.auth.api_keys import api_key_token_from_header

            auth_header = request.headers.get("Authorization", "")
            raw_bearer = api_key_token_from_header(auth_header)
            if raw_bearer is not None:
                return await run_in_session(_require_api_key_context, raw_bearer, request)

            # Match required authentication: the desktop shell owns the local
            # identity, even when a forwarded cloud cookie is absent or stale.
            bridge_user = await _resolve_host_user(request)
            if bridge_user is not None:
                return bridge_user

            # ── Try Cookie session first (all modes) ──
            try:
                session_user = await _resolve_session_user(request)
                if session_user is not None:
                    return session_user
            except HTTPException:
                # Session cookie expired — for optional auth, return None
                return None

            # ── session mode: no Cookie = no user ──
            if auth_mode == "session":
                return None

            # ── mock / remote: try Bearer ──
            if not auth_header or not auth_header.startswith("Bearer "):
                if auth_mode != "mock":
                    return None
                token = MOCK_TOKEN
            else:
                token = auth_header.split(" ", 1)[1]
            try:
                return await _resolve_bearer_user(token, audit=False)
            except AccountCapacityExceeded:
                # Edition admission blocks must not degrade to anonymous access.
                raise
            except Exception as e:
                logger.warning("optional_auth_resolve_failed", error=str(e))
                return None

        return optional_user
