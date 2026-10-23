"""Environment-backed configuration values."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from .environment import _env, _bool, _int, _float

@dataclass(frozen=True)
class LLMSettings:
    model_url: str = field(default_factory=lambda: _env("MODEL_URL", ""))
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))
    base_model_name: str = field(default_factory=lambda: _env("BASE_MODEL_NAME", ""))
    enable_summary: bool = field(default_factory=lambda: _bool(_env("ENABLE_SUMMARY", "true")))
    summary_max_rounds: int = field(
        default_factory=lambda: _int(_env("SUMMARY_MAX_ROUNDS", "3"), 3)
    )

@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = field(default_factory=lambda: _bool(_env("MEM0_ENABLED", "false")))
    graph_enabled: bool = field(default_factory=lambda: _bool(_env("MEM0_GRAPH_ENABLED", "false")))
    embed_url: str = field(default_factory=lambda: _env("MEM0_EMBED_URL", ""))
    embed_model: str = field(default_factory=lambda: _env("MEM0_EMBED_MODEL", "qwen3_embedding_8b"))
    embed_api_key: str = field(default_factory=lambda: _env("MEM0_EMBED_API_KEY", "sk-placeholder"))
    embed_dims: int = field(default_factory=lambda: _int(_env("MEM0_EMBED_DIMS", "1024"), 1024))
    model_name: str = field(
        default_factory=lambda: _env("MEMORY_MODEL_NAME", _env("BASE_MODEL_NAME", "deepseek-chat"))
    )
    model_url: str = field(default_factory=lambda: _env("MEMORY_MODEL_URL", _env("MODEL_URL", "")))
    api_key: str = field(
        default_factory=lambda: _env("MEMORY_API_KEY", _env("API_KEY", "sk-placeholder"))
    )
    milvus_url: str = field(default_factory=lambda: _env("MILVUS_URL", "http://milvus:19530"))
    milvus_token: str = field(default_factory=lambda: _env("MILVUS_TOKEN", ""))
    neo4j_url: str = field(default_factory=lambda: _env("NEO4J_URL", "bolt://neo4j:7687"))
    neo4j_username: str = field(default_factory=lambda: _env("NEO4J_USERNAME", "neo4j"))
    neo4j_password: str = field(
        default_factory=lambda: _env("NEO4J_PASSWORD", "hugagent_neo4j_2026")
    )

    # ── Layered memory additions ─────────────────────────────────
    layered_enabled: bool = field(
        default_factory=lambda: _bool(_env("MEMORY_LAYERED_ENABLED", "true"))
    )
    audit_enabled: bool = field(default_factory=lambda: _bool(_env("MEMORY_AUDIT_ENABLED", "true")))
    retrieval_budget_ms: int = field(
        default_factory=lambda: _int(_env("MEMORY_RETRIEVAL_BUDGET_MS", "600"), 600)
    )
    bg_max_concurrency: int = field(
        default_factory=lambda: _int(_env("MEMORY_BG_MAX_CONCURRENCY", "8"), 8)
    )
    outbox_lease_s: int = field(
        default_factory=lambda: _int(_env("MEMORY_OUTBOX_LEASE_SECONDS", "120"), 120)
    )
    outbox_max_attempts: int = field(
        default_factory=lambda: _int(_env("MEMORY_OUTBOX_MAX_ATTEMPTS", "5"), 5)
    )
    outbox_retry_base_s: int = field(
        default_factory=lambda: _int(_env("MEMORY_OUTBOX_RETRY_BASE_SECONDS", "5"), 5)
    )
    outbox_poll_interval_s: float = field(
        default_factory=lambda: _float(_env("MEMORY_OUTBOX_POLL_SECONDS", "1"), 1.0)
    )
    extract_timeout_s: int = field(
        default_factory=lambda: _int(_env("MEMORY_EXTRACT_TIMEOUT_S", "30"), 30)
    )
    profile_max_chars: int = field(
        default_factory=lambda: _int(_env("MEMORY_PROFILE_MAX_CHARS", "1500"), 1500)
    )
    fact_default_ttl_days: int = field(
        default_factory=lambda: _int(_env("MEMORY_FACT_DEFAULT_TTL_DAYS", "180"), 180)
    )
    # ── Write-path quality gates ─────────────────────────────────
    # One fast LLM call after the regex classify that decides whether the turn
    # actually contains anything worth remembering. The regex classes are the
    # recall floor; the gate can only narrow them, never widen.
    llm_gate_enabled: bool = field(
        default_factory=lambda: _bool(_env("MEMORY_LLM_GATE_ENABLED", "true"))
    )
    gate_timeout_s: int = field(
        default_factory=lambda: _int(_env("MEMORY_GATE_TIMEOUT_S", "10"), 10)
    )
    # Near-duplicate threshold for L2 procedures: a new rule whose cosine score
    # against an existing entry reaches this is a reinforcement, not a new row.
    procedure_dedup_min_score: float = field(
        default_factory=lambda: _float(_env("MEMORY_PROCEDURE_DEDUP_MIN_SCORE", "0.9"), 0.9)
    )
    # Lifetimes: strong rules (or any rule seen twice) persist for a year;
    # a weak rule enters provisionally and ages out unless it recurs.
    procedure_ttl_days: int = field(
        default_factory=lambda: _int(_env("MEMORY_PROCEDURE_TTL_DAYS", "365"), 365)
    )
    procedure_weak_ttl_days: int = field(
        default_factory=lambda: _int(_env("MEMORY_PROCEDURE_WEAK_TTL_DAYS", "30"), 30)
    )
    frozen_topk: int = field(default_factory=lambda: _int(_env("MEMORY_FROZEN_TOPK", "5"), 5))
    breaker_threshold: int = field(
        default_factory=lambda: _int(_env("MEMORY_BREAKER_THRESHOLD", "3"), 3)
    )
    breaker_cooldown_s: int = field(
        default_factory=lambda: _int(_env("MEMORY_BREAKER_COOLDOWN_S", "60"), 60)
    )

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
    detail_content_max_chars: int = field(
        default_factory=lambda: _int(_env("KB_DETAIL_CONTENT_MAX_CHARS", "50000"), 50000)
    )
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
    socket_timeout: int = field(
        default_factory=lambda: _int(_env("REDIS_SOCKET_TIMEOUT", "30"), 30)
    )

@dataclass(frozen=True)
class ServerSettings:
    env: str = field(default_factory=lambda: _env("ENV", "dev").lower())
    port: int = field(
        default_factory=lambda: _int(_env("PORT", _env("BACKEND_PORT", "3001")), 3001)
    )
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", ""))
    mcp_oauth_public_base_url: str = field(
        default_factory=lambda: _env("MCP_OAUTH_PUBLIC_BASE_URL", "").rstrip("/")
    )
    max_request_size: int = field(
        default_factory=lambda: _int(
            _env("MAX_REQUEST_SIZE", str(50 * 1024 * 1024)), 50 * 1024 * 1024
        )
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    log_file_path: str = field(
        default_factory=lambda: (_env("LOG_FILE_PATH") or "/app/logs/backend.log").strip()
    )
    log_file_max_bytes: int = field(
        default_factory=lambda: _int((_env("LOG_FILE_MAX_BYTES") or "10485760").strip(), 10485760)
    )
    log_file_backup_count: int = field(
        default_factory=lambda: _int((_env("LOG_FILE_BACKUP_COUNT") or "5").strip(), 5)
    )
    # Hostname of the dedicated `mcp` container — every MCP server is
    # reached at ``http://<mcp_host>:<port>/mcp/``. Defaults to the docker
    # service name; override with MCP_HOST=127.0.0.1 for local debugging
    # (e.g. running ``python -m extensions.mcp_servers._launcher`` outside docker).
    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "mcp"))
    mcp_market_revalidate_interval: int = field(
        default_factory=lambda: max(
            300,
            _int(_env("MCP_MARKET_REVALIDATE_INTERVAL", "21600"), 21600),
        )
    )
    # Self-service MCP endpoints are public-only by default to preserve the
    # SSRF boundary. Private deployments may opt in when trusted users need to
    # connect MCP services hosted on the same LAN or Docker network.
    mcp_self_service_allow_private_network: bool = field(
        default_factory=lambda: _bool(_env("MCP_SELF_SERVICE_ALLOW_PRIVATE_NETWORK", "false"))
    )

    @property
    def is_prod(self) -> bool:
        return self.env in ("prod", "production")

@dataclass(frozen=True)
class RoutingSettings:
    strategy: str = field(
        default_factory=lambda: (_env("ROUTER_STRATEGY") or "main_only").strip().lower()
    )
    followup_enabled: bool = field(default_factory=lambda: _bool(_env("FOLLOWUP_ENABLED", "true")))

@dataclass(frozen=True)
class CompactionSettings:
    """Context compaction (see core/llm/context/compaction.py).

    Compresses history into "recent user messages + summary" and persists the
    summary as a checkpoint. One implementation serves all three phases
    (mid-turn / pre-turn / post-turn, see ``core.llm.context.compaction.CompactionPhase``),
    so every knob below governs all of them — there is no separate in-turn ratio
    any more.
    """

    # Master switch. When off, replay falls back to the original full-history path (no checkpoint consumption/generation).
    enabled: bool = field(default_factory=lambda: _bool(_env("CHAT_COMPACT_ENABLED", "true")))
    # Trigger threshold (real prompt tokens). <=0 means auto-derive from the model window (see compaction_service).
    token_limit: int = field(default_factory=lambda: _int(_env("CHAT_COMPACT_TOKEN_LIMIT", "0"), 0))
    # The single fraction of the model context window used when auto-deriving the
    # threshold — shared by every phase, so a mid-turn and a pre-turn check can no
    # longer disagree about whether the same history is over budget.
    # ⚠️ Runtime authority is chat.compress_in_turn_ratio in the Config admin panel
    # ("System Config → context"), read per turn by
    # ``compaction_service.resolve_trigger_ratio``; the envs here are only the
    # default. ``CHAT_COMPRESS_IN_TURN_RATIO`` is the deprecated alias kept so
    # deployments that only ever tuned the in-turn knob keep their tuning.
    trigger_ratio: float = field(
        default_factory=lambda: float(
            _env("CHAT_COMPACT_TRIGGER_RATIO", "")
            or _env("CHAT_COMPRESS_IN_TURN_RATIO", "0.9")
            or "0.9"
        )
    )
    # Tokens of recent history kept verbatim after compaction: the tail of
    # complete steps that stays in front of the model while everything older
    # goes into the summary.
    keep_recent_tokens: int = field(
        default_factory=lambda: _int(_env("CHAT_COMPACT_RECENT_USER_MAX_TOKENS", "20000"), 20000)
    )
    # Summary LLM call timeout (seconds).
    summarize_timeout_s: int = field(
        default_factory=lambda: _int(_env("CHAT_COMPACT_SUMMARIZE_TIMEOUT_S", "60"), 60)
    )
    # Per-tool-result cap fed to AgentScope's ContextConfig. This is the
    # deterministic, model-free layer that runs before any summarization (the
    # counterpart of Codex's output truncation and dsh's toolResultPruner);
    # the overflow is spilled to the sandbox by core/llm/context/offloader.py.
    tool_result_limit: int = field(
        default_factory=lambda: _int(_env("CHAT_TOOL_RESULT_LIMIT", "20000"), 20000)
    )

@dataclass(frozen=True)
class PromptSettings:
    provider: str = field(
        default_factory=lambda: (_env("PROMPT_PROVIDER") or "filesystem").strip().lower()
    )
    dir: str = field(default_factory=lambda: _env("PROMPT_DIR", ""))
    inline_template: str = field(default_factory=lambda: _env("PROMPT_INLINE_TEMPLATE", ""))
    config_path: str = field(default_factory=lambda: _env("JX_PROMPT_CONFIG", ""))
