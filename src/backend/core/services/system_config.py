"""Central service configuration service (DB-driven, cached).

Manages external service configs (DB query, KB, industry, file parser) stored
in the system_configs table. Thread-safe singleton with a short TTL cache so
admin changes take effect within seconds without a restart.

Falls back to os.getenv() when DB has no value for a given key.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from core.db.engine import SessionLocal
from core.db.models import SystemConfig
from core.services.edition_system_config import CONFIG_KEY_TO_ENV as EDITION_CONFIG_KEY_TO_ENV
from core.services.edition_system_config import SEED_CONFIGS as EDITION_SEED_CONFIGS

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30.0

# ── Seed definitions (single source of truth) ────────────────────────────────
# (config_key, default_value, display_name, description, group_key, is_secret)
SEED_CONFIGS: list[tuple[str, str | None, str, str, str, bool]] = [
    # query_database
    (
        "query_database.url",
        None,
        "数据库查询接口 URL",
        "NL2SQL 数据库查询服务地址",
        "query_database",
        False,
    ),
    ("query_database.timeout", "40", "超时时间(秒)", "请求超时时间", "query_database", False),
    ("query_database.retry_times", "1", "重试次数", "请求失败重试次数", "query_database", False),
    (
        "query_database.max_output_tokens",
        "45000",
        "最大输出 Token",
        "数据库查询最大输出 Token 数",
        "query_database",
        False,
    ),
    (
        "database_query.capability_enabled",
        "true",
        "数据库查询能力总开关",
        "控制统一「数据库查询」能力是否在能力中心可用。由数据库工具页维护，不写入 catalog.json。",
        "query_database",
        False,
    ),
    (
        "knowledge_base.detail_max_chars",
        "50000",
        "详情最大字符数",
        "知识库文档详情最大字符数",
        "knowledge_base",
        False,
    ),
    # industry
    ("industry.url", None, "产业知识中心 URL", "产业链信息接口地址", "industry", False),
    ("industry.auth_token", None, "产业知识中心 Token", "产业链信息接口鉴权令牌", "industry", True),
    # file_parser
    ("file_parser.api_url", None, "文件解析 API URL", "PDF/文档解析服务地址", "file_parser", False),
    ("file_parser.timeout", "60", "超时时间(秒)", "文件解析请求超时", "file_parser", False),
    ("file_parser.lang_list", "ch", "语言列表", "OCR 语言列表", "file_parser", False),
    ("file_parser.backend", "pipeline", "解析后端", "解析后端引擎", "file_parser", False),
    ("file_parser.parse_method", "auto", "解析方法", "auto / ocr / txt", "file_parser", False),
    (
        "file_parser.formula_enable",
        "true",
        "启用公式识别",
        "是否启用公式识别",
        "file_parser",
        False,
    ),
    ("file_parser.table_enable", "true", "启用表格识别", "是否启用表格识别", "file_parser", False),
    # internet_search
    (
        "internet_search.engine",
        "tavily",
        "搜索引擎",
        "互联网搜索引擎 (tavily / baidu)",
        "internet_search",
        False,
    ),
    (
        "internet_search.tavily_api_key",
        None,
        "Tavily API Key",
        "Tavily 互联网搜索服务密钥",
        "internet_search",
        True,
    ),
    (
        "internet_search.baidu_api_key",
        None,
        "百度搜索 API Key",
        "百度千帆 AppBuilder 搜索服务密钥",
        "internet_search",
        True,
    ),
    # sandbox / code execution. The admin toggle is the **sole authority** (no env fallback):
    # explicitly defaults to "true"; admins can turn it off in "System config", which writes "false" and persists.
    (
        "sandbox.code_capability_enable",
        "true",
        "主智能体代码执行能力",
        "开启后主/计划/批量/子智能体默认具备文件工具+沙箱+我的空间访问；关闭则仅 Lab "
        "入口可用。本开关为唯一控制源，保存后 ≤30s 生效、无需重启。",
        "sandbox",
        False,
    ),
    # context —— tuning for conversation context compression. In-turn compression (AgentScope ContextConfig) trigger ratio:
    # compress history when estimated tokens exceed model context window × this value. Admin config takes precedence over env
    # (CHAT_COMPRESS_IN_TURN_RATIO is only the default); takes effect on new conversations after saving, no restart needed.
    # ⚠️ Grouped under context, not chat: the chat group is in service_configs._HIDDEN_GROUPS
    # (chat.user_model_switch_enabled is exclusively managed by the model management panel to prevent a double entry point),
    # so attaching to the chat group would hide the whole group in the system config UI.
    (
        "chat.compress_in_turn_ratio",
        "0.82",
        "轮内压缩触发比例",
        "上下文估算 token 超过「模型窗口 × 该值」时触发轮内压缩（0.5~0.95）。计数用"
        "字节估算：中文会被高估（实际触发比标称晚）、英文/代码接近真实。调大=更晚压缩"
        "（上下文更完整，但英文/代码密集会话有触顶风险）；调小=更早压缩。",
        "context",
        False,
    ),
    # dingtalk —— DingTalk workbench Custom App (enterprise self-built app) credentials. Once AppKey/AppSecret are filled,
    # DingTalk login and the dws command inside the sandbox execute as that self-built app, bypassing DingTalk's co-creation-era
    # "CLI data access" org toggle and self-controlling scope; leave empty to use the dws default shared app (org admin must
    # enable CLI data access first). Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart.
    (
        "dingtalk.client_id",
        None,
        "钉钉 AppKey (client-id)",
        "企业自建钉钉应用的 AppKey。在 open-dev.dingtalk.com 创建企业内部应用后获取。",
        "dingtalk",
        True,
    ),
    (
        "dingtalk.client_secret",
        None,
        "钉钉 AppSecret (client-secret)",
        "企业自建钉钉应用的 AppSecret，与 AppKey 配对。",
        "dingtalk",
        True,
    ),
    (
        "dingtalk.trusted_domains",
        "*.dingtalk.com",
        "受信域名白名单",
        "dws bearer token 允许发送的域名，逗号分隔，默认锁死 *.dingtalk.com 防 token 外泄。",
        "dingtalk",
        False,
    ),
    # firecrawl —— deployment-level credentials for the firecrawl web scraping/retrieval CLI. The firecrawl CLI is preinstalled
    # in the sandbox; at runtime these two items are injected as FIRECRAWL_API_KEY / FIRECRAWL_API_URL env vars into
    # every user's sandbox commands (one shared config for all users, mirroring the DingTalk dws Custom App approach).
    # - Cloud version: fill only api_key (starts with fc-).
    # - Self-hosted: fill api_url pointing to the self-built instance (the CLI skips cloud auth once it sees api_url); if the
    #   self-built instance also needs a key, fill both.
    # Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart.
    (
        "firecrawl.api_key",
        None,
        "Firecrawl API Key",
        "firecrawl 云版 API Key（fc- 开头），在 firecrawl.dev 注册获取。用自托管实例时可留空。",
        "firecrawl",
        True,
    ),
    (
        "firecrawl.api_url",
        None,
        "Firecrawl 自托管地址",
        "自托管 firecrawl 实例地址（如 http://firecrawl:3002）。填了即走自建实例、CLI 跳过云端鉴权；用云版时留空。",
        "firecrawl",
        False,
    ),
    # lark —— Feishu workbench app (enterprise self-built app) credentials. Once AppID/AppSecret are filled, the lark-cli inside
    # the sandbox uses that self-built app as its OAuth app: admin configures once, and each user completes the device-flow
    # auth login to operate Feishu as themselves. The app config is seeded by the backend into each user's lark HOME
    # (config init), not injected as a global env var — this avoids lark-cli defaulting to the tenant/bot token flow when
    # LARK_APP_* is in the environment, which would break --as user. Sole authority = admin config, ≤30s after saving, no restart.
    (
        "lark.app_id",
        None,
        "飞书 App ID",
        "企业自建飞书应用的 App ID。在 open.feishu.cn 创建企业自建应用后获取。",
        "lark",
        True,
    ),
    (
        "lark.app_secret",
        None,
        "飞书 App Secret",
        "企业自建飞书应用的 App Secret，与 App ID 配对。",
        "lark",
        True,
    ),
    # auth —— login and session. Number of days a session is kept after the user checks "remember login state" (checked by
    # default) on the login page. Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart;
    # if unchecked, the default session duration (SESSION_TTL_HOURS) is used.
    (
        "auth.remember_me_days",
        "30",
        "记住登录状态时长(天)",
        "用户在登录页勾选「记住登录状态」后，登录会话保持的天数（默认 30 天）。"
        "未勾选则使用默认会话时长。",
        "auth",
        False,
    ),
] + EDITION_SEED_CONFIGS

# config_key → env var name mapping
_CONFIG_KEY_TO_ENV: dict[str, str] = {
    "query_database.url": "QUERY_DATABASE_URL",
    "query_database.timeout": "QUERY_DATABASE_TIMEOUT_SECONDS",
    "query_database.retry_times": "QUERY_DATABASE_RETRY_TIMES",
    "query_database.max_output_tokens": "QUERY_DATABASE_MAX_OUTPUT_TOKENS",
    "knowledge_base.detail_max_chars": "KB_DETAIL_CONTENT_MAX_CHARS",
    "industry.url": "INDUSTRY_URL",
    "industry.auth_token": "INDUSTRY_AUTH_TOKEN",
    "file_parser.api_url": "FILE_PARSER_API_URL",
    "file_parser.timeout": "FILE_PARSER_TIMEOUT",
    "file_parser.lang_list": "FILE_PARSER_LANG_LIST",
    "file_parser.backend": "FILE_PARSER_BACKEND",
    "file_parser.parse_method": "FILE_PARSER_PARSE_METHOD",
    "file_parser.formula_enable": "FILE_PARSER_FORMULA_ENABLE",
    "file_parser.table_enable": "FILE_PARSER_TABLE_ENABLE",
    "internet_search.engine": "INTERNET_SEARCH_ENGINE",
    "internet_search.tavily_api_key": "TAVILY_API_KEY",
    "internet_search.baidu_api_key": "BAIDU_API_KEY",
    # Note: sandbox.code_capability_enable deliberately does **not** map to an env var —— the admin toggle is the sole
    # authority (plan B), explicitly seeded to "true", and env CODE_CAPABILITY_ENABLED is retired.
    **EDITION_CONFIG_KEY_TO_ENV,
}

# Reverse mapping for env-fallback lookups
_ENV_TO_CONFIG_KEY: dict[str, str] = {v: k for k, v in _CONFIG_KEY_TO_ENV.items()}


def get_config_key_for_env(env_var: str) -> Optional[str]:
    """Return the system-config key that backs ``env_var``, if any.

    Public lookup helper so callers (e.g. ``runtime_env``) don't need to import
    the internal reverse map.
    """
    return _ENV_TO_CONFIG_KEY.get(env_var)


class SystemConfigService:
    """Thread-safe singleton that resolves service configs from DB with env fallback."""

    _instance: Optional["SystemConfigService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._cache: dict[str, Optional[str]] = {}
        self._cache_meta: dict[str, dict] = {}  # full row metadata
        self._cache_ts: float = 0.0
        self._cache_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "SystemConfigService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── get / get_group ─────────────────────────────────────────────

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a config value. DB first, then env fallback, then default."""
        self._maybe_refresh()
        if key in self._cache and self._cache[key] is not None:
            return self._cache[key]
        # env fallback
        env_key = _CONFIG_KEY_TO_ENV.get(key)
        if env_key:
            env_val = os.getenv(env_key)
            if env_val is not None:
                return env_val.strip()
        return default

    def get_group(self, group_key: str) -> dict[str, str]:
        """Return all config key→value pairs for a group."""
        self._maybe_refresh()
        result: dict[str, str] = {}
        for key, meta in self._cache_meta.items():
            if meta.get("group_key") == group_key:
                val = self.get(key)
                if val is not None:
                    result[key] = val
        return result

    def get_all_configs(self) -> list[dict]:
        """Return all config rows as dicts (for API responses)."""
        self._maybe_refresh()
        return list(self._cache_meta.values())

    def get_group_configs(self, group_key: str) -> list[dict]:
        """Return config rows for a specific group."""
        self._maybe_refresh()
        return [m for m in self._cache_meta.values() if m.get("group_key") == group_key]

    # ── set ─────────────────────────────────────────────────────────

    def set(self, key: str, value: str | None, updated_by: str = "admin") -> None:
        """Update a config value in DB."""
        try:
            db = SessionLocal()
            try:
                row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
                if row is None:
                    logger.warning("[SystemConfigService] key %s not found, skipping set", key)
                    return
                row.config_value = value
                row.updated_by = updated_by
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("[SystemConfigService] set(%s) failed: %s", key, exc)
            raise
        self.invalidate_cache()

    def bulk_set(self, items: list[dict], updated_by: str = "admin") -> None:
        """Batch update multiple config values. Each item: {key, value}.

        For secret fields, masked values (containing '****') are skipped to
        prevent the frontend from accidentally overwriting real secrets with
        their masked representation.
        """
        try:
            db = SessionLocal()
            try:
                for item in items:
                    key = item.get("key", "").strip()
                    value = item.get("value")
                    if not key:
                        continue
                    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
                    if row is None:
                        continue
                    # Skip masked values for secret fields
                    if row.is_secret and isinstance(value, str) and "****" in value:
                        continue
                    row.config_value = value if value != "" else None
                    row.updated_by = updated_by
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("[SystemConfigService] bulk_set failed: %s", exc)
            raise
        self.invalidate_cache()

    # ── cache management ──────────────────────────────────────────

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._cache_meta.clear()
            self._cache_ts = 0.0

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._cache_ts < _CACHE_TTL_SECONDS and self._cache_meta:
            return
        with self._cache_lock:
            if now - self._cache_ts < _CACHE_TTL_SECONDS and self._cache_meta:
                return
            self._load_from_db()
            self._cache_ts = time.monotonic()

    def _ensure_seed_rows(self, db) -> None:
        """Insert any missing seed config rows. Idempotent."""
        existing_keys = {r[0] for r in db.query(SystemConfig.config_key).all()}
        inserted = 0
        for key, val, display, desc, group, secret in SEED_CONFIGS:
            if key not in existing_keys:
                db.add(
                    SystemConfig(
                        config_key=key,
                        config_value=val,
                        display_name=display,
                        description=desc,
                        group_key=group,
                        is_secret=secret,
                    )
                )
                inserted += 1
        if inserted:
            db.commit()
            logger.info("[SystemConfigService] Inserted %d missing seed row(s)", inserted)

    def _load_from_db(self) -> None:
        new_cache: dict[str, Optional[str]] = {}
        new_meta: dict[str, dict] = {}
        try:
            db = SessionLocal()
            try:
                self._ensure_seed_rows(db)
                rows = db.query(SystemConfig).all()
                for row in rows:
                    new_cache[row.config_key] = row.config_value
                    new_meta[row.config_key] = {
                        "config_key": row.config_key,
                        "config_value": row.config_value,
                        "display_name": row.display_name,
                        "description": row.description,
                        "group_key": row.group_key,
                        "is_secret": row.is_secret,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "updated_by": row.updated_by,
                    }
            finally:
                db.close()
        except Exception as exc:
            logger.warning("[SystemConfigService] DB load failed, keeping stale cache: %s", exc)
            return

        self._cache = new_cache
        self._cache_meta = new_meta

    # ── env overlay for MCP sub-processes ─────────────────────────

    def get_service_env_overlay(self) -> dict[str, str]:
        """Return env-var style dict for injecting into MCP sub-processes.

        Maps DB configs to the env var names that MCP servers already read via os.getenv().
        Only includes keys that have a non-None value (from DB or env fallback).
        """
        overlay: dict[str, str] = {}
        for config_key, env_key in _CONFIG_KEY_TO_ENV.items():
            val = self.get(config_key)
            if val is not None:
                overlay[env_key] = val
        return overlay


def code_capability_enabled() -> bool:
    """Master switch for the main agent's code execution capability (single source of truth at runtime).

    Sole control source = the DB value of Config console "System config" (plan B, env fallback retired);
    seeded explicitly to "true". Saving in the console calls invalidate_cache; takes effect ≤30s without restart.
    agent_factory and the admin_prompts preview call the same source, ensuring the console display matches runtime.
    """
    try:
        val = SystemConfigService.get_instance().get("sandbox.code_capability_enable", "false")
    except Exception:  # noqa: BLE001 — conservatively disable on config-layer errors
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def auto_plan_entry_enabled() -> bool:
    """Master switch for the main agent auto-entering plan mode (the enter_plan_mode tool).

    Control source = the Config console "System config" DB value ``chat.auto_plan_entry_enable``,
    defaulting to "true" (enabled by default). Ops can set it to false to disable the capability, ≤30s, no restart.
    Conservatively disable on config-layer errors (don't register the tool, fall back to manually clicking "Plan mode").
    """
    try:
        val = SystemConfigService.get_instance().get("chat.auto_plan_entry_enable", "true")
    except Exception:  # noqa: BLE001 — conservatively disable on config-layer errors
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")
