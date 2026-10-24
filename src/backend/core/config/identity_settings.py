"""Environment-backed configuration values."""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional
from .environment import _env, _bool, _int

@dataclass(frozen=True)
class AuthSettings:
    mode: str = field(default_factory=lambda: _env("AUTH_MODE", "mock"))
    api_url: str = field(default_factory=lambda: _env("AUTH_API_URL", ""))
    api_timeout: int = field(default_factory=lambda: _int(_env("AUTH_API_TIMEOUT", "5"), 5))
    retry_count: int = field(default_factory=lambda: _int(_env("AUTH_RETRY_COUNT", "2"), 2))
    mock_user_id: str = field(default_factory=lambda: _env("AUTH_MOCK_USER_ID", "dev_user_001"))
    mock_username: str = field(default_factory=lambda: _env("AUTH_MOCK_USERNAME", "Developer"))
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))
    config_token: str = field(default_factory=lambda: _env("CONFIG_TOKEN", ""))

    # Local user system (self-managed accounts + invite codes + teams)
    local_enabled: bool = field(default_factory=lambda: _bool(_env("LOCAL_AUTH_ENABLED", "true")))
    password_min_length: int = field(
        default_factory=lambda: _int(_env("PASSWORD_MIN_LENGTH", "8"), 8)
    )
    invite_code_default_ttl_hours: int = field(
        default_factory=lambda: _int(_env("INVITE_CODE_DEFAULT_TTL_HOURS", "168"), 168)
    )

@dataclass(frozen=True)
class SSOSettings:
    login_url: str = field(default_factory=lambda: _env("SSO_LOGIN_URL", ""))
    login_mode: str = field(default_factory=lambda: _env("SSO_LOGIN_MODE", "").lower())
    mock_enabled: bool = field(default_factory=lambda: _bool(_env("SSO_MOCK_ENABLED", "false")))
    exchange_mode: str = field(default_factory=lambda: _env("SSO_EXCHANGE_MODE", "").lower())
    ticket_exchange_url: str = field(default_factory=lambda: _env("SSO_TICKET_EXCHANGE_URL", ""))
    login_provider_url: str = field(default_factory=lambda: _env("SSO_LOGIN_PROVIDER_URL", ""))
    logout_url: str = field(default_factory=lambda: _env("SSO_LOGOUT_URL", ""))
    callback_param: str = field(
        default_factory=lambda: _env("SSO_CALLBACK_PARAM", "ticket").strip().lower() or "ticket"
    )
    timeout: int = field(default_factory=lambda: _int(_env("SSO_TIMEOUT_SECONDS", "5"), 5))

    @property
    def effective_login_mode(self) -> str:
        """Normalized login-page mode: local / mock / remote.

        When not set explicitly:
          - SSO_MOCK_ENABLED=true → mock (backward compat with old configs)
          - otherwise             → local (defaults to the self-managed account system)
        """
        mode = (self.login_mode or "").strip().lower()
        if mode in ("local", "mock", "remote"):
            return mode
        if self.mock_enabled:
            return "mock"
        return "local"

    @property
    def effective_login_url(self) -> str:
        """The actual URL the frontend redirects to on 401. An explicitly configured SSO_LOGIN_URL takes precedence."""
        if self.login_url:
            return self.login_url
        mode = self.effective_login_mode
        if mode == "local":
            return "/login"
        if mode == "mock":
            return "/mock-sso/login"
        # remote but no URL configured: return empty string and let the caller handle it
        return ""

@dataclass(frozen=True)
class SessionSettings:
    cookie_name: str = field(default_factory=lambda: _env("SESSION_COOKIE_NAME", "jx_session"))
    cookie_secure: bool = field(
        default_factory=lambda: _bool(_env("SESSION_COOKIE_SECURE", "false"))
    )
    cookie_samesite: str = field(default_factory=lambda: _env("SESSION_COOKIE_SAMESITE", "lax"))
    cookie_domain: Optional[str] = field(
        default_factory=lambda: _env("SESSION_COOKIE_DOMAIN", "") or None
    )
    cookie_httponly: bool = field(
        default_factory=lambda: _bool(_env("SESSION_COOKIE_HTTPONLY", "false"))
    )
    ttl_hours: float = field(default_factory=lambda: float(_env("SESSION_TTL_HOURS", "8")))
    store_type: str = field(default_factory=lambda: _env("SESSION_STORE", "memory").lower().strip())

@dataclass(frozen=True)
class OASsoSettings:
    """OA single sign-on (server-side direct-push provisioning).

    Integration shape: the OA backend pushes an authenticated user's
    ``user_id`` + ``dept_id`` + signature to this platform; after signature
    verification the platform auto-creates a local account (username =
    user_id, random strong password), binds a team by dept_id (default role:
    member), and issues a session token for OA redirect login. This is a
    separate path from SSOSettings' ticket verification — here the trust
    anchor is on the OA side, with server-to-server mutual trust established
    via HMAC signature verification.
    """

    enabled: bool = field(default_factory=lambda: _bool(_env("OA_SSO_ENABLED", "false")))
    # HMAC shared secret: when configured, signature verification is enforced (recommended); if empty, verification is skipped (intranet integration testing only, logs a warning)
    sign_secret: str = field(default_factory=lambda: _env("OA_SSO_SIGN_SECRET", ""))
    # Timestamp tolerance (seconds) — requests outside the window are rejected as replays
    sign_ttl_seconds: int = field(
        default_factory=lambda: _int(_env("OA_SSO_SIGN_TTL_SECONDS", "300"), 300)
    )
    # Default role for new users joining the organization team
    default_role: str = field(
        default_factory=lambda: _env("OA_SSO_DEFAULT_ROLE", "member").strip() or "member"
    )
    # One-time login ticket TTL (seconds) — used for the browser redirect-to-session exchange; shorter is safer
    ticket_ttl_seconds: int = field(
        default_factory=lambda: _int(_env("OA_SSO_TICKET_TTL_SECONDS", "60"), 60)
    )
    # Page path the callback 302s to after establishing the session
    redirect_path: str = field(
        default_factory=lambda: _env("OA_SSO_REDIRECT_PATH", "/").strip() or "/"
    )

@dataclass(frozen=True)
class DatabaseSettings:
    # Default/fallback SQLite DBs go in the system temp dir (absolute path) — a
    # relative path follows the process CWD and leaves stray .db files in the
    # repo root / src/backend
    url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", f"sqlite:///{tempfile.gettempdir()}/hugagent.db"
        )
    )
    sqlite_fallback_url: str = field(
        default_factory=lambda: _env(
            "SQLITE_FALLBACK_URL", f"sqlite:///{tempfile.gettempdir()}/hugagent_dev.db"
        )
    )
    echo: bool = field(default_factory=lambda: _bool(_env("DB_ECHO", "false")))
    # Threads per process that run blocking work for requests (sync routes and
    # dependencies, ``core.db.threads.run_db``). The connection pool is derived
    # from it in ``core.db.engine.thread_budget`` rather than configured apart.
    worker_threads: int = field(default_factory=lambda: max(1, _int(_env("WORKER_THREADS", "40"), 40)))
    pool_timeout: int = field(default_factory=lambda: _int(_env("DB_POOL_TIMEOUT", "30"), 30))
