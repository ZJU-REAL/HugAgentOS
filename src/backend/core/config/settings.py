"""Centralized application settings.

All environment variables are read here once at import time.
Other modules should ``from core.config.settings import settings`` instead
of calling ``os.getenv()`` directly.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values


_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_env_files() -> None:
    """Load repo-level env files without overriding real process env vars.

    Precedence:
    1. Existing process environment
    2. Env-specific file such as .env.dev
    3. Base .env
    """
    base_env_path = _REPO_ROOT / ".env"
    base_values = dotenv_values(base_env_path) if base_env_path.exists() else {}

    resolved_env = (
        os.getenv("ENV")
        or os.getenv("ENVIRONMENT")
        or str(base_values.get("ENV") or "")
        or str(base_values.get("ENVIRONMENT") or "")
    ).strip().lower()

    candidate_paths = [base_env_path]
    if resolved_env:
        candidate_paths.append(_REPO_ROOT / f".env.{resolved_env}")
    elif (_REPO_ROOT / ".env.dev").exists():
        candidate_paths.append(_REPO_ROOT / ".env.dev")

    merged_values: dict[str, str] = {}
    for path in candidate_paths:
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                merged_values[key] = value

    for key, value in merged_values.items():
        os.environ.setdefault(key, value)


_load_env_files()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


def _int(val: str, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# Legacy frontend model alias, stamped on runs/requests when no explicit model
# name is resolved. Runtime model selection is DB-driven (model providers /
# role assignments); this is only the last-resort label. Single source of
# truth — do not inline "qwen" elsewhere.
DEFAULT_CHAT_MODEL_ALIAS = "qwen"


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
    password_min_length: int = field(default_factory=lambda: _int(_env("PASSWORD_MIN_LENGTH", "8"), 8))
    invite_code_default_ttl_hours: int = field(default_factory=lambda: _int(_env("INVITE_CODE_DEFAULT_TTL_HOURS", "168"), 168))


@dataclass(frozen=True)
class SSOSettings:
    login_url: str = field(default_factory=lambda: _env("SSO_LOGIN_URL", ""))
    login_mode: str = field(default_factory=lambda: _env("SSO_LOGIN_MODE", "").lower())
    mock_enabled: bool = field(default_factory=lambda: _bool(_env("SSO_MOCK_ENABLED", "false")))
    exchange_mode: str = field(default_factory=lambda: _env("SSO_EXCHANGE_MODE", "").lower())
    ticket_exchange_url: str = field(default_factory=lambda: _env("SSO_TICKET_EXCHANGE_URL", ""))
    login_provider_url: str = field(default_factory=lambda: _env("SSO_LOGIN_PROVIDER_URL", ""))
    logout_url: str = field(default_factory=lambda: _env("SSO_LOGOUT_URL", ""))
    callback_param: str = field(default_factory=lambda: _env("SSO_CALLBACK_PARAM", "ticket").strip().lower() or "ticket")
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
    cookie_secure: bool = field(default_factory=lambda: _bool(_env("SESSION_COOKIE_SECURE", "false")))
    cookie_samesite: str = field(default_factory=lambda: _env("SESSION_COOKIE_SAMESITE", "lax"))
    cookie_domain: Optional[str] = field(default_factory=lambda: _env("SESSION_COOKIE_DOMAIN", "") or None)
    cookie_httponly: bool = field(default_factory=lambda: _bool(_env("SESSION_COOKIE_HTTPONLY", "false")))
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
    sign_ttl_seconds: int = field(default_factory=lambda: _int(_env("OA_SSO_SIGN_TTL_SECONDS", "300"), 300))
    # Default role for new users joining the organization team
    default_role: str = field(default_factory=lambda: _env("OA_SSO_DEFAULT_ROLE", "member").strip() or "member")
    # One-time login ticket TTL (seconds) — used for the browser redirect-to-session exchange; shorter is safer
    ticket_ttl_seconds: int = field(default_factory=lambda: _int(_env("OA_SSO_TICKET_TTL_SECONDS", "60"), 60))
    # Page path the callback 302s to after establishing the session
    redirect_path: str = field(default_factory=lambda: _env("OA_SSO_REDIRECT_PATH", "/").strip() or "/")


@dataclass(frozen=True)
class DatabaseSettings:
    # Default/fallback SQLite DBs go in the system temp dir (absolute path) — a
    # relative path follows the process CWD and leaves stray .db files in the
    # repo root / src/backend
    url: str = field(default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{tempfile.gettempdir()}/hugagent.db"))
    sqlite_fallback_url: str = field(default_factory=lambda: _env("SQLITE_FALLBACK_URL", f"sqlite:///{tempfile.gettempdir()}/hugagent_dev.db"))
    echo: bool = field(default_factory=lambda: _bool(_env("DB_ECHO", "false")))
    pool_size: int = field(default_factory=lambda: _int(_env("DB_POOL_SIZE", "20"), 20))
    pool_max_overflow: int = field(default_factory=lambda: _int(_env("DB_POOL_MAX_OVERFLOW", "10"), 10))
    pool_timeout: int = field(default_factory=lambda: _int(_env("DB_POOL_TIMEOUT", "30"), 30))


@dataclass(frozen=True)
class LLMSettings:
    model_url: str = field(default_factory=lambda: _env("MODEL_URL", ""))
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))
    base_model_name: str = field(default_factory=lambda: _env("BASE_MODEL_NAME", ""))
    enable_summary: bool = field(default_factory=lambda: _bool(_env("ENABLE_SUMMARY", "true")))
    summary_max_rounds: int = field(default_factory=lambda: _int(_env("SUMMARY_MAX_ROUNDS", "3"), 3))


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = field(default_factory=lambda: _bool(_env("MEM0_ENABLED", "false")))
    graph_enabled: bool = field(default_factory=lambda: _bool(_env("MEM0_GRAPH_ENABLED", "false")))
    embed_url: str = field(default_factory=lambda: _env("MEM0_EMBED_URL", ""))
    embed_model: str = field(default_factory=lambda: _env("MEM0_EMBED_MODEL", "qwen3_embedding_8b"))
    embed_api_key: str = field(default_factory=lambda: _env("MEM0_EMBED_API_KEY", "sk-placeholder"))
    embed_dims: int = field(default_factory=lambda: _int(_env("MEM0_EMBED_DIMS", "1024"), 1024))
    model_name: str = field(default_factory=lambda: _env("MEMORY_MODEL_NAME", _env("BASE_MODEL_NAME", "deepseek-chat")))
    model_url: str = field(default_factory=lambda: _env("MEMORY_MODEL_URL", _env("MODEL_URL", "")))
    api_key: str = field(default_factory=lambda: _env("MEMORY_API_KEY", _env("API_KEY", "sk-placeholder")))
    milvus_url: str = field(default_factory=lambda: _env("MILVUS_URL", "http://milvus:19530"))
    milvus_token: str = field(default_factory=lambda: _env("MILVUS_TOKEN", ""))
    neo4j_url: str = field(default_factory=lambda: _env("NEO4J_URL", "bolt://neo4j:7687"))
    neo4j_username: str = field(default_factory=lambda: _env("NEO4J_USERNAME", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD", "hugagent_neo4j_2026"))

    # ── Layered memory additions ─────────────────────────────────
    layered_enabled: bool = field(default_factory=lambda: _bool(_env("MEMORY_LAYERED_ENABLED", "true")))
    audit_enabled: bool = field(default_factory=lambda: _bool(_env("MEMORY_AUDIT_ENABLED", "true")))
    retrieval_budget_ms: int = field(default_factory=lambda: _int(_env("MEMORY_RETRIEVAL_BUDGET_MS", "600"), 600))
    bg_max_concurrency: int = field(default_factory=lambda: _int(_env("MEMORY_BG_MAX_CONCURRENCY", "8"), 8))
    extract_timeout_s: int = field(default_factory=lambda: _int(_env("MEMORY_EXTRACT_TIMEOUT_S", "30"), 30))
    profile_max_chars: int = field(default_factory=lambda: _int(_env("MEMORY_PROFILE_MAX_CHARS", "1500"), 1500))
    fact_default_ttl_days: int = field(default_factory=lambda: _int(_env("MEMORY_FACT_DEFAULT_TTL_DAYS", "180"), 180))
    frozen_topk: int = field(default_factory=lambda: _int(_env("MEMORY_FROZEN_TOPK", "5"), 5))
    breaker_threshold: int = field(default_factory=lambda: _int(_env("MEMORY_BREAKER_THRESHOLD", "3"), 3))
    breaker_cooldown_s: int = field(default_factory=lambda: _int(_env("MEMORY_BREAKER_COOLDOWN_S", "60"), 60))


@dataclass(frozen=True)
class StorageSettings:
    type: str = field(default_factory=lambda: _env("STORAGE_TYPE", "local").lower())
    path: str = field(default_factory=lambda: _env("STORAGE_PATH", "").strip())

    @property
    def root(self) -> Path:
        """Storage root as a Path; single fallback for an unset STORAGE_PATH.

        Use this instead of re-spelling ``settings.storage.path or "/app/storage"``
        at call sites.
        """
        return Path(self.path or "/app/storage")


@dataclass(frozen=True)
class KnowledgeBaseSettings:
    backend: str = field(default_factory=lambda: (_env("KNOWLEDGE_BASE") or "").strip().lower())
    dify_url: str = field(default_factory=lambda: _env("DIFY_URL") or _env("DIFY_BASE_URL") or "")
    dify_api_key: str = field(default_factory=lambda: _env("DIFY_API_KEY") or _env("DIFY_AUTH_TOKEN") or "")
    dify_allowed_dataset_ids: str = field(default_factory=lambda: (_env("DIFY_ALLOWED_DATASET_IDS") or "").strip())
    detail_content_max_chars: int = field(default_factory=lambda: _int(_env("KB_DETAIL_CONTENT_MAX_CHARS", "50000"), 50000))
    reranker_url: str = field(default_factory=lambda: _env("RERANKER_URL", "").rstrip("/"))
    reranker_model: str = field(default_factory=lambda: _env("RERANKER_MODEL", ""))
    reranker_api_key: str = field(default_factory=lambda: _env("RERANKER_API_KEY", ""))


@dataclass(frozen=True)
class RedisSettings:
    url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://redis:6379/0"))
    # Socket read timeout (seconds). MUST stay comfortably above the longest
    # blocking command we issue — the chat-stream follower uses `XREAD BLOCK
    # 5000` (5s). redis-py 8.0 changed the default socket_timeout from None to
    # 5s, so the default would fire at the exact instant the 5s XREAD returns
    # its nil reply → spurious "Timeout reading from redis" on every idle
    # window. 30s gives a 25s safety margin while still catching dead sockets.
    socket_timeout: int = field(default_factory=lambda: _int(_env("REDIS_SOCKET_TIMEOUT", "30"), 30))


@dataclass(frozen=True)
class ServerSettings:
    env: str = field(default_factory=lambda: _env("ENV", "dev").lower())
    port: int = field(default_factory=lambda: _int(_env("PORT", _env("BACKEND_PORT", "3001")), 3001))
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", ""))
    max_request_size: int = field(default_factory=lambda: _int(_env("MAX_REQUEST_SIZE", str(50 * 1024 * 1024)), 50 * 1024 * 1024))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    log_file_path: str = field(default_factory=lambda: (_env("LOG_FILE_PATH") or "/app/logs/backend.log").strip())
    log_file_max_bytes: int = field(default_factory=lambda: _int((_env("LOG_FILE_MAX_BYTES") or "10485760").strip(), 10485760))
    log_file_backup_count: int = field(default_factory=lambda: _int((_env("LOG_FILE_BACKUP_COUNT") or "5").strip(), 5))
    # Hostname of the dedicated `mcp` container — every MCP server is
    # reached at ``http://<mcp_host>:<port>/mcp/``. Defaults to the docker
    # service name; override with MCP_HOST=127.0.0.1 for local debugging
    # (e.g. running ``python -m mcp_servers._launcher`` outside docker).
    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "mcp"))

    @property
    def is_prod(self) -> bool:
        return self.env in ("prod", "production")


@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool = field(default_factory=lambda: _bool(_env("RATE_LIMIT_ENABLED", "true")))
    storage: str = field(default_factory=lambda: _env("RATE_LIMIT_STORAGE", "memory://"))
    cb_user_center_threshold: int = field(default_factory=lambda: _int(_env("CB_USER_CENTER_THRESHOLD", "5"), 5))
    cb_user_center_timeout: int = field(default_factory=lambda: _int(_env("CB_USER_CENTER_TIMEOUT", "60"), 60))
    cb_model_api_threshold: int = field(default_factory=lambda: _int(_env("CB_MODEL_API_THRESHOLD", "10"), 10))
    cb_model_api_timeout: int = field(default_factory=lambda: _int(_env("CB_MODEL_API_TIMEOUT", "30"), 30))
    cb_storage_threshold: int = field(default_factory=lambda: _int(_env("CB_STORAGE_THRESHOLD", "5"), 5))
    cb_storage_timeout: int = field(default_factory=lambda: _int(_env("CB_STORAGE_TIMEOUT", "60"), 60))


@dataclass(frozen=True)
class RoutingSettings:
    strategy: str = field(default_factory=lambda: (_env("ROUTER_STRATEGY") or "main_only").strip().lower())
    followup_enabled: bool = field(default_factory=lambda: _bool(_env("FOLLOWUP_ENABLED", "true")))


@dataclass(frozen=True)
class CompactionSettings:
    """Context compaction (see core/llm/compaction.py).

    Across turns, compresses history into "recent user messages + summary";
    the summary is persisted to the DB and carried into subsequent turns,
    instead of recompressing from raw history every turn.
    """

    # Master switch. When off, replay falls back to the original full-history path (no checkpoint consumption/generation).
    enabled: bool = field(default_factory=lambda: _bool(_env("CHAT_COMPACT_ENABLED", "true")))
    # Trigger threshold (real prompt tokens). <=0 means auto-derive from the model window (see compaction_service).
    token_limit: int = field(default_factory=lambda: _int(_env("CHAT_COMPACT_TOKEN_LIMIT", "0"), 0))
    # Fraction of the model context window used when auto-deriving the threshold.
    trigger_ratio: float = field(
        default_factory=lambda: float(_env("CHAT_COMPACT_TRIGGER_RATIO", "0.8") or "0.8")
    )
    # Token budget for the recent user messages kept after compaction.
    recent_user_max_tokens: int = field(
        default_factory=lambda: _int(_env("CHAT_COMPACT_RECENT_USER_MAX_TOKENS", "20000"), 20000)
    )
    # Summary LLM call timeout (seconds).
    summarize_timeout_s: int = field(
        default_factory=lambda: _int(_env("CHAT_COMPACT_SUMMARIZE_TIMEOUT_S", "60"), 60)
    )
    # AS2 in-turn compaction (agent_factory ContextConfig) trigger ratio:
    # compress when estimated tokens exceed model window × this value. Note the
    # metric differs from the cross-turn compaction trigger_ratio above: this
    # one uses AS2's utf-8 bytes/4 estimate (overestimates Chinese; close to
    # real for English/code), while cross-turn uses real usage values. The
    # historical 0.6 was a conservative value compensating for the Chinese
    # overestimate, but it fired too early in practice (compaction started
    # while real usage was far below threshold); the default is now 0.82.
    # ⚠️ Runtime authority is chat.compress_in_turn_ratio in the Config admin
    # panel "System Config → Chat & Context Compaction" (DB, takes effect on
    # save); this env is only the default fallback.
    in_turn_trigger_ratio: float = field(
        default_factory=lambda: float(_env("CHAT_COMPRESS_IN_TURN_RATIO", "0.82") or "0.82")
    )


@dataclass(frozen=True)
class PromptSettings:
    provider: str = field(default_factory=lambda: (_env("PROMPT_PROVIDER") or "filesystem").strip().lower())
    dir: str = field(default_factory=lambda: _env("PROMPT_DIR", ""))
    inline_template: str = field(default_factory=lambda: _env("PROMPT_INLINE_TEMPLATE", ""))
    config_path: str = field(default_factory=lambda: _env("JX_PROMPT_CONFIG", ""))


@dataclass(frozen=True)
class IndustrySettings:
    url: str = field(default_factory=lambda: _env("INDUSTRY_URL", ""))
    auth_token: str = field(default_factory=lambda: _env("INDUSTRY_AUTH_TOKEN", ""))


@dataclass(frozen=True)
class SandboxSettings:
    """Sandbox provider configuration.

    ``SANDBOX_PROVIDER`` switches the underlying execution environment:
    - ``script_runner`` (default): built-in Docker container + setrlimit subprocess
    - ``opensandbox``: Alibaba OpenSandbox (Docker container + persistent Jupyter context)
    - ``cube``: Tencent CubeSandbox (external E2B-compatible MicroVM node)
    """

    provider: str = field(default_factory=lambda: _env("SANDBOX_PROVIDER", "script_runner").strip().lower())

    # script_runner sidecar call parameters. Defaults to the compose service name (not the container name), decoupled from container renames.
    runner_url: str = field(default_factory=lambda: _env("SANDBOX_RUNNER_URL", "http://script-runner:8900"))
    enabled: bool = field(default_factory=lambda: _bool(_env("SANDBOX_TOOLS_ENABLED", "false")))
    default_timeout: int = field(default_factory=lambda: _int(_env("SANDBOX_TOOLS_TIMEOUT", "30"), 30))
    max_timeout: int = field(default_factory=lambda: _int(_env("SANDBOX_TOOLS_MAX_TIMEOUT", "120"), 120))

    # opensandbox
    opensandbox_domain: str = field(default_factory=lambda: _env("OPENSANDBOX_DOMAIN", "http://opensandbox:8080"))
    opensandbox_api_key: str = field(default_factory=lambda: _env("OPENSANDBOX_API_KEY", ""))
    opensandbox_image: str = field(default_factory=lambda: _env("OPENSANDBOX_IMAGE", "opensandbox/code-interpreter:v1.0.2"))
    opensandbox_default_timeout_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_DEFAULT_TIMEOUT_S", "1800"), 1800))
    opensandbox_ready_timeout_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_READY_TIMEOUT_S", "90"), 90))
    opensandbox_request_timeout_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_REQUEST_TIMEOUT_S", "120"), 120))
    # Direct execd connection: bypasses the OpenSandbox server's proxy
    # forwarding (measured ~3s extra buffering overhead per request — file
    # read/write 3s→<5ms, commands 4s→1s). Connects directly to the sandbox
    # container sandbox-<id>:44772; requires backend and sandbox containers to
    # be reachable on the same docker network (true for this project's compose
    # topology). A one-time reachability probe runs at creation; if
    # unreachable, it auto-falls back to the server proxy with zero feature loss.
    opensandbox_direct_execd_enabled: bool = field(default_factory=lambda: _bool(_env("OPENSANDBOX_DIRECT_EXECD", "true")))
    # endpoint fastpath: skips the server's GET /endpoints/{port} (measured
    # fixed ~3.3s hard server-side latency; Sandbox.create calls it once each
    # for execd+egress → saves ~6.6s cold start). The returned proxy endpoint
    # is a deterministic string constructible locally. Enabled only in insecure
    # mode (empty api_key): with an api_key the server may stuff auth/routing
    # headers into the endpoint that cannot be replicated locally.
    opensandbox_endpoint_fastpath_enabled: bool = field(default_factory=lambda: _bool(_env("OPENSANDBOX_ENDPOINT_FASTPATH", "true")))
    # Warm pool: fill each bucket to min_idle right after process startup so the first user gets a warm sandbox
    opensandbox_pool_jupyter_min_idle: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_JUPYTER_MIN_IDLE", "2"), 2))
    opensandbox_pool_jupyter_max_idle: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_JUPYTER_MAX_IDLE", "3"), 3))
    opensandbox_pool_light_min_idle: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIGHT_MIN_IDLE", "2"), 2))
    opensandbox_pool_light_max_idle: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIGHT_MAX_IDLE", "5"), 5))
    opensandbox_pool_max_total: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_MAX_TOTAL", "20"), 20))
    # Liveness probe (GET /sandboxes/{id}) timeout (seconds) before taking an
    # idle sandbox out of the pool. Unreachable/timeout does not delete it —
    # left for acquire / the next round to handle. See
    # _OpenSandboxSessionMixin._probe_pooled_sandbox_alive.
    opensandbox_pool_liveness_probe_timeout_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_POOL_LIVENESS_PROBE_TIMEOUT_S", "5"), 5))
    # Active idle-reap threshold for persistent sessions (seconds). A chat-level
    # persistent sandbox with no business requests beyond this value is
    # destroyed by a background task instead of waiting for the server-side
    # 30min TTL. <=0 disables active reaping. A hard prerequisite for keeping
    # the pool from being saturated by idle sessions once file tools are
    # enabled in all modes (see docs/code-execution-merge-proposal §4.2).
    opensandbox_idle_reap_threshold_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_IDLE_REAP_S", "600"), 600))
    # ─── Snapshot persistence (see internal design docs) ─────────────
    # Master switch: true → enable snapshot park/restore + background worker;
    # false → fall back to the status quo (idle sandboxes are lost when reaped;
    # on reconnect, bash returns 404 if the sandbox is already dead).
    opensandbox_snapshot_enabled: bool = field(default_factory=lambda: _bool(_env("OPENSANDBOX_SNAPSHOT_ENABLED", "true")))
    # When a session is idle beyond this value (seconds), the background worker
    # proactively snapshots + kills it to free resources. Default was 300s
    # (5min). Must be < opensandbox_default_timeout_s (1800), otherwise GC gets
    # there first. Plan F Q2 coordination: default raised from 300s (5 min) to
    # 1500s (25 min), aligned with the OpenSandbox server-side sandbox TTL
    # (opensandbox_default_timeout_s default 1800s = 30 min). With the old
    # value, a typical user reading a response for 5 minutes got repeatedly
    # snapshot+killed and rebuilt (measured ~21s restore) — very poor UX. The
    # new value gives the Q2 idle pool ample time to handle short-term reuse.
    opensandbox_idle_snapshot_threshold_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S", "1500"), 1500))
    # Snapshot retention in the DB (days); expired ones are deleted by the GC worker (DB row + opensandbox side).
    opensandbox_snapshot_retention_days: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_SNAPSHOT_RETENTION_DAYS", "7"), 7))
    # Max polling time (seconds) waiting for snapshot accept→Ready. Measured ~60s; 120s gives a 1× safety margin.
    opensandbox_snapshot_wait_timeout_s: int = field(default_factory=lambda: _int(_env("OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S", "120"), 120))
    # Skill dependencies come only from the image bake (docker/Dockerfile.opensandbox); new dependencies go through the admin-panel rebuild.

    # The all-modes code-execution capability switch has moved out of settings:
    # the sole control source is sandbox.code_capability_enable in the Config
    # admin panel "System Config" (option B, see
    # core.services.system_config.code_capability_enabled). The env is retired.
    # Hard safety for user confirmation of MySpace write operations (docs §13).
    # true (default): Write/Edit/Delete/Move on /myspace must pass out-of-band
    # user confirmation; non-interactive mode (batch/sub-agent) rejects writes
    # outright. Ops can set false to disable (trusted-path policy soft
    # constraint). With code_capability enabled this is the key protection
    # against the main agent accidentally modifying the user's private drive.
    myspace_write_confirm: bool = field(default_factory=lambda: _bool(_env("MYSPACE_WRITE_CONFIRM", "true")))

    # User confirmation for automation (cron task) changes (borrows the §13
    # MySpace write-confirm suspend gate). true (default): when the Agent
    # creates/edits/deletes cron tasks via the automation plugin
    # (automation_task MCP), out-of-band user confirmation is required in web
    # interactive chats; channel (IM bot) runs and non-interactive modes
    # (batch/sub-agent/plan execution/scheduler-triggered) skip the prompt and
    # pass directly. Set false to disable.
    automation_write_confirm: bool = field(default_factory=lambda: _bool(_env("AUTOMATION_WRITE_CONFIRM", "true")))

    # ─── Plan F: direct myspace_cache bind-mount ───────────────────────────
    # true (default): use an OpenSandbox host Volume to bind the backend's
    # myspace_cache/{uid} subdirectory into the sandbox at
    # /workspace/myspace/{uid}/ — visible at startup, no HTTP PUT sync
    # overhead. Disabling falls back to the old path (_sync_inputs_to_sandbox
    # full PUT + materialize over HTTP).
    opensandbox_myspace_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED", "true")),
    )
    # Bind source path: the real location of the backend storage directory on
    # the host (docker-compose's ${HOST_STORAGE_PATH}). The OpenSandbox server
    # side must add it to allowed_host_paths. Must exactly match the host path
    # docker-compose uses when bind-mounting ${HOST_STORAGE_PATH}:/app/storage
    # — so the backend's /app/storage/myspace_cache/{uid}/ and the sandbox's
    # /workspace/myspace/{uid}/ point at the same host inode.
    opensandbox_host_storage_path: str = field(
        default_factory=lambda: _env("HOST_STORAGE_PATH", "").strip(),
    )

    # ─── dws (DingTalk Workspace CLI) integration ─────────────────────────
    # The dingtalk marketplace skill runs dws inside the sandbox under the
    # current user's DingTalk identity. Credentials (per-user OAuth) live on a
    # per-user bind-mount persistent volume (~/.dws + ~/.local/share/dws-cli),
    # surviving across sessions. This is an **architecture switch** (part of
    # the sandbox mechanism), so it stays in settings; the Custom App's
    # client-id/secret/trusted_domains are **operational config**, stored via
    # the Config admin platform "System Config → DingTalk Workspace" DB config
    # (dingtalk.* system-config keys), not in .env/compose. See
    # internal design docs.
    dws_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("DWS_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── lark-cli (Feishu/Lark CLI) integration ──────────────────────────
    # The feishu-cli marketplace plugin's lark-* skills run lark-cli inside the
    # sandbox under the current user's Feishu identity. Credentials (per-user
    # OAuth, file-based encryption) live on a per-user bind-mount persistent
    # volume (~/.lark-cli + ~/.local/share/lark-cli, including master.key),
    # surviving across sessions. Architecture switch, stays in settings; the
    # app's app_id/app_secret are operational config via "System Config →
    # Feishu Workspace" DB config (lark.* keys). See
    # internal design docs.
    lark_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("LARK_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── himalaya (email CLI) integration ────────────────────────────────
    # The email marketplace plugin's email skill runs himalaya inside the
    # sandbox to send/manage mail under the current user's mailbox identity.
    # Credentials (per-user IMAP/SMTP app passwords, plain file-based
    # config.toml) live on a per-user bind-mount persistent volume
    # (~/.config/himalaya), surviving across sessions. Architecture switch,
    # stays in settings; the mailbox address/app password is **user-provided**
    # (not deployment-level), written by the backend into the per-user
    # config.toml — not in system config, not in .env. See
    # internal design docs.
    email_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("EMAIL_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # ─── openyida (Yida low-code platform CLI) integration ──────────────
    # The yida marketplace plugin's skills use the openyida CLI inside the
    # sandbox to operate Yida (apps/forms/pages/workflows/reports etc.). The
    # login state is a plain file cookie (<workdir>/.cache/cookies-*.json,
    # written by QR-code login); a per-user bind-mount persistent volume mounts
    # the fixed working directory ~/yida-workspace into the sandbox, surviving
    # across sessions. Unlike dws/lark/email: login completes inside the chat
    # (openyida login --agent-qr shows a QR code, user scans with DingTalk) —
    # no backend subprocess login, no DB connection table, no system-config keys.
    yida_creds_bind_mount_enabled: bool = field(
        default_factory=lambda: _bool(_env("YIDA_CREDS_BIND_MOUNT_ENABLED", "true")),
    )

    # cube (Tencent CubeSandbox, E2B-compatible MicroVM; external node, no local sidecar needed)
    cube_api_url: str = field(default_factory=lambda: _env("CUBE_API_URL", "http://cube-node:38473"))
    cube_api_key: str = field(default_factory=lambda: _env("CUBE_API_KEY", ""))
    # Data-plane sandbox domain, may include a port (when cube-proxy is not on 443); the SDK builds https://{port}-{id}.{domain} from it
    cube_api_sandbox_domain: str = field(default_factory=lambda: _env("CUBE_API_SANDBOX_DOMAIN", "cube.app:38573"))
    # Required: sandbox template id (CubeSandbox requires it when creating a sandbox)
    cube_template: str = field(default_factory=lambda: _env("CUBE_TEMPLATE", "").strip())
    # Sandbox TTL (seconds); CubeMaster does not yet support set_timeout renewal, so this is the at-creation upper bound
    cube_default_timeout_s: int = field(default_factory=lambda: _int(_env("CUBE_DEFAULT_TIMEOUT_S", "1800"), 1800))
    cube_request_timeout_s: int = field(default_factory=lambda: _int(_env("CUBE_REQUEST_TIMEOUT_S", "120"), 120))
    # mkcert rootCA bundle (in-container path); when non-empty, injects SSL_CERT_FILE so the SDK trusts the self-signed cert
    cube_ca_bundle: str = field(default_factory=lambda: _env("CUBE_CA_BUNDLE", "").strip())
    # Active idle-reap threshold for session sandboxes (seconds); <=0 disables active reaping (rely on CubeSandbox's built-in TTL)
    cube_idle_reap_threshold_s: int = field(default_factory=lambda: _int(_env("CUBE_IDLE_REAP_S", "600"), 600))
    # Warm-pool target idle count: refilled in the background to this value on
    # startup / after each take, so a new session's first run gets a warm
    # sandbox, skipping AsyncSandbox.create's MicroVM cold start (~10s). <=0
    # disables the warm pool.
    cube_pool_min_idle: int = field(default_factory=lambda: _int(_env("CUBE_POOL_MIN_IDLE", "2"), 2))
    # Sandbox owner tag: written into metadata["hugagent-owner"], used so the
    # startup orphan sweep only recognizes this environment's sandboxes.
    # CubeMaster (MVP) does not honor the sandbox TTL; a backend restart loses
    # session/pool in-memory state and orphaned sandboxes linger on the node.
    # At startup, sandboxes of this environment are listed by this tag and
    # orphans not in the registry are cleaned. When multiple environments share
    # the same cube node, **each must set a unique value** (e.g. hugagent-dev /
    # hugagent-test / hugagent-test), otherwise the sweep would kill other
    # environments' sandboxes. Empty: metadata still writes "backend", but the
    # startup orphan sweep is **disabled** (the safe default on shared nodes).
    cube_owner_tag: str = field(default_factory=lambda: _env("CUBE_OWNER_TAG", "").strip())

    # ─── Skill on-demand push optimization (bundled transfer + session-bind pre-push) ─────
    # On-demand skill pushes (built-in + DB) switch to "tar bundle → single
    # upload → extract inside the sandbox", avoiding pushing tens of thousands
    # of small files one by one. Pre-push: when a session first binds a warm
    # sandbox, enabled skills are pre-pushed in the background, overlapping the
    # first LLM turn to hide latency; only skills whose tar is ≤ max_mb are
    # pre-pushed (oversized skills like ppt-master remain on-demand, paying one
    # upload only when actually used — avoids wasting tens of MB of pushes on
    # text-only sessions).
    cube_skill_prepush: bool = field(default_factory=lambda: _bool(_env("CUBE_SKILL_PREPUSH", "true")))
    cube_skill_prepush_max_mb: int = field(default_factory=lambda: _int(_env("CUBE_SKILL_PREPUSH_MAX_MB", "20"), 20))
    cube_skill_prepush_concurrency: int = field(default_factory=lambda: _int(_env("CUBE_SKILL_PREPUSH_CONCURRENCY", "3"), 3))

    # ─── Cube remote template rebuild (the admin "Sandbox deps → App deps" path when provider=cube) ──
    # The backend does not build locally; instead it scps the aggregated
    # dependency manifest to the cube node + triggers via ssh a docker build on
    # the node → push to the local registry → cubemastercli create-from-image,
    # then hot-swaps _template with the new template id + writes it back to
    # .env. An empty host means "remote rebuild not configured": the rebuild
    # endpoint errors out for cube and directs users to the manual docs path.
    # See core/services/cube_template_builder.py for details.
    cube_node_ssh_host: str = field(default_factory=lambda: _env("CUBE_NODE_SSH_HOST", _env("CUBE_NODE_IP", "")).strip())
    cube_node_ssh_port: int = field(default_factory=lambda: _int(_env("CUBE_NODE_SSH_PORT", "22"), 22))
    cube_node_ssh_user: str = field(default_factory=lambda: _env("CUBE_NODE_SSH_USER", "root").strip())
    # In-container private key path (docker-compose mounts the host key read-only); empty uses the default ssh key chain
    cube_node_ssh_key: str = field(default_factory=lambda: _env("CUBE_NODE_SSH_KEY", "").strip())
    # Build context directory on the node (Dockerfile.cube-sandbox and dependency manifests are scp'd flat into it; the src tree is rsync'd on first deployment)
    cube_build_ctx_dir: str = field(default_factory=lambda: _env("CUBE_BUILD_CTX_DIR", "/opt/cube-build").strip())
    cube_build_image_tag: str = field(default_factory=lambda: _env("CUBE_BUILD_IMAGE_TAG", "hugagent-cube-sandbox:latest").strip())
    cube_build_registry: str = field(default_factory=lambda: _env("CUBE_BUILD_REGISTRY", "127.0.0.1:5000").strip())
    # create-from-image resource/port parameters (aligned with the existing READY template)
    cube_build_writable_layer: str = field(default_factory=lambda: _env("CUBE_BUILD_WRITABLE_LAYER", "8Gi").strip())
    cube_build_cpu: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_CPU", "2000"), 2000))
    cube_build_memory: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_MEMORY", "4000"), 4000))
    # Comma-separated exposed ports; probe port + path
    cube_build_expose_ports: str = field(default_factory=lambda: _env("CUBE_BUILD_EXPOSE_PORTS", "49983,49999").strip())
    cube_build_probe_port: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_PROBE_PORT", "49999"), 49999))
    cube_build_probe_path: str = field(default_factory=lambda: _env("CUBE_BUILD_PROBE_PATH", "/health").strip())
    # Separate timeout ceilings for build / registration (seconds)
    cube_build_timeout_s: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_TIMEOUT_S", "1800"), 1800))
    cube_build_register_timeout_s: int = field(default_factory=lambda: _int(_env("CUBE_BUILD_REGISTER_TIMEOUT_S", "900"), 900))

    # General
    max_concurrent: int = field(default_factory=lambda: _int(_env("SANDBOX_MAX_CONCURRENT", "4"), 4))


@dataclass(frozen=True)
class EditionSettings:
    """Edition facade (CE/EE). Main repo defaults to ee; the CE derived tree sets JX_EDITION=ce via .env."""
    edition: str = field(default_factory=lambda: _env("JX_EDITION", "ee").strip().lower() or "ee")

    @property
    def is_ee(self) -> bool:
        return self.edition == "ee"


@dataclass(frozen=True)
class LicenseSettings:
    """Offline license (Ed25519-signed file, verified in-process — no license service, no online dependency).

    - ``license_key_path``: path to the license file; when unset and
      ``required=False``, runs fully featured as an "internal deployment"
      (compatible with the current fully-managed / test-machine setups).
    - ``required=True``: private-delivery mode — without a valid license all EE
      capability bits are disabled.
    - ``grace_days``: post-expiry grace period (days); features are retained
      during grace while probes raise alerts.
    """
    license_key_path: str = field(default_factory=lambda: _env("LICENSE_KEY_PATH", "").strip())
    public_key: str = field(default_factory=lambda: _env("LICENSE_PUBLIC_KEY", "").strip())
    required: bool = field(default_factory=lambda: _bool(_env("JX_LICENSE_REQUIRED", "false")))
    grace_days: int = field(default_factory=lambda: _int(_env("LICENSE_GRACE_DAYS", "14"), 14))


@dataclass(frozen=True)
class GatewaySettings:
    """External model gateway (LiteLLM Proxy data plane).

    The data plane is a standalone LiteLLM container (exposing an
    OpenAI-compatible endpoint); this process is the **control plane**, using
    the LiteLLM admin API (authenticated with ``master_key``) to issue/revoke
    virtual keys and read spend. ``master_key`` is held by the backend only and
    is never sent to the frontend. Without ``master_key`` configured, the
    control-plane endpoints return errors.
    """
    enabled: bool = field(default_factory=lambda: _bool(_env("MODEL_GATEWAY_ENABLED", "false")))
    admin_url: str = field(default_factory=lambda: _env("LITELLM_ADMIN_URL", "http://litellm:4000").strip().rstrip("/"))
    master_key: str = field(default_factory=lambda: _env("LITELLM_MASTER_KEY", "").strip())
    timeout: int = field(default_factory=lambda: _int(_env("MODEL_GATEWAY_TIMEOUT", "15"), 15))


@dataclass(frozen=True)
class BrandingSettings:
    """Single source of brand defaults — the in-code fallback stays neutral; deployment branding comes from env / the content_blocks DB seed."""
    product_name: str = field(default_factory=lambda: _env("BRAND_PRODUCT_NAME", "智能体平台").strip())
    org_name: str = field(default_factory=lambda: _env("BRAND_ORG_NAME", "").strip())
    powered_by_visible: bool = field(default_factory=lambda: _bool(_env("BRAND_POWERED_BY", "true")))


@dataclass(frozen=True)
class DeploySettings:
    """Deployment-profile switch.

    ``DEPLOY_PROFILE=local`` = Docker-free single-machine mode (hermes-style
    quick install): a single uvicorn process hosting frontend statics + SQLite
    + in-process fakeredis + subprocess MCP/sandbox. Default empty string =
    regular compose deployment; no local behavior activates, zero impact on
    existing deployments. Decoupled from capability-specific envs
    (``REDIS_URL=memory://`` / ``DATABASE_URL=sqlite://`` /
    ``SANDBOX_PROVIDER=script_runner``): this switch only toggles **profile**
    behaviors like "in-process hosted sub-services + frontend static mounting";
    each capability is still explicitly driven by its own env (written by the CLI).
    """
    profile: str = field(default_factory=lambda: _env("DEPLOY_PROFILE", "").strip().lower())

    @property
    def is_local(self) -> bool:
        return self.profile == "local"


@dataclass(frozen=True)
class AppSettings:
    """Top-level settings container — one read from env at startup."""
    deploy: DeploySettings = field(default_factory=DeploySettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    sso: SSOSettings = field(default_factory=SSOSettings)
    oa_sso: OASsoSettings = field(default_factory=OASsoSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    db: DatabaseSettings = field(default_factory=DatabaseSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    kb: KnowledgeBaseSettings = field(default_factory=KnowledgeBaseSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    compaction: CompactionSettings = field(default_factory=CompactionSettings)
    prompt: PromptSettings = field(default_factory=PromptSettings)
    industry: IndustrySettings = field(default_factory=IndustrySettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    edition: EditionSettings = field(default_factory=EditionSettings)
    license: LicenseSettings = field(default_factory=LicenseSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    branding: BrandingSettings = field(default_factory=BrandingSettings)


# Singleton — import this everywhere
settings = AppSettings()
