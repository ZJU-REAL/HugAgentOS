"""MCP server configuration service (DB-driven, cached).

Reads MCP server configs from the admin_mcp_servers table and provides
them in the same dict format that MCPConnectionPool and agent_factory
expect (compatible with the old MCP_SERVERS dict from mcp_config.py).

Thread-safe singleton with a 30s TTL cache.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer

logger = logging.getLogger(__name__)

_CACHE_TTL = 30.0


def _rewrite_builtin_mcp_host(url: str) -> str:
    """Point built-in MCP URLs at the configured host.

    Plugin-provided MCP servers (automation_task / skill_manager / site_publish)
    hardcode the Docker service name in their manifests
    (``http://mcp:<port>/mcp/``). That host only resolves inside the compose
    network, so in the no-Docker local profile (``MCP_HOST=127.0.0.1``) the
    plugin's tool calls would be unreachable. Rewrite **only** the literal ``mcp``
    host to ``settings.server.mcp_host``: a no-op on compose (mcp_host == "mcp"),
    loopback in local. External/user-added remote MCPs have real hostnames and are
    left untouched.
    """
    if not url or "//mcp:" not in url and "//mcp/" not in url:
        return url
    from urllib.parse import urlparse, urlunparse
    from core.config.settings import settings

    try:
        parts = urlparse(url)
        if parts.hostname != "mcp":
            return url
        host = settings.server.mcp_host
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunparse(parts._replace(netloc=netloc))
    except Exception:
        return url


class McpServerConfigService:
    """Reads MCP server configs from DB with in-memory caching."""

    _instance: Optional[McpServerConfigService] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._cache: Optional[Dict[str, dict]] = None
        self._cache_all: Optional[Dict[str, dict]] = None  # includes disabled
        self._cache_ts: float = 0.0
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> McpServerConfigService:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_all_servers(self, enabled_only: bool = True) -> Dict[str, dict]:
        """Return {server_id: config_dict} from DB, cached for 30s.

        The config_dict format is compatible with the old MCP_SERVERS dict:
        {
            "transport": "stdio",
            "command": "python",
            "args": [...],
            "env": {...},       # merged: env_inherit from OS + env_vars
            "url": "...",       # for HTTP/SSE
            "headers": {...},
            "is_stable": True,
        }
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < _CACHE_TTL:
            return dict(self._cache) if enabled_only else dict(self._cache_all or self._cache)

        with self._lock:
            # Double-check after acquiring lock
            if self._cache is not None and (time.monotonic() - self._cache_ts) < _CACHE_TTL:
                return dict(self._cache) if enabled_only else dict(self._cache_all or self._cache)

            return self._load_from_db(enabled_only)

    def _load_from_db(self, enabled_only: bool) -> Dict[str, dict]:
        """Load all servers from DB and rebuild cache."""
        enabled_map: Dict[str, dict] = {}
        all_map: Dict[str, dict] = {}

        try:
            with SessionLocal() as session:
                # The global cache holds only public MCPs (owner_user_id is null). Private MCPs users add themselves
                # do not enter the global pool/connection pool, to avoid leaking to other users; at runtime get_owned_servers
                # loads and injects them per user_id separately.
                # Secondary sort key server_id: when sort_order ties (all 0 by default) Postgres row order
                # is not guaranteed stable, and any change in tool order busts the LLM prefix cache → the order must be deterministic.
                rows = (
                    session.query(AdminMcpServer)
                    .filter(AdminMcpServer.owner_user_id.is_(None))
                    .order_by(AdminMcpServer.sort_order, AdminMcpServer.server_id)
                    .all()
                )
                for row in rows:
                    if is_removed_builtin_mcp_server(
                        row.server_id,
                        source_plugin=row.source_plugin,
                    ):
                        continue
                    cfg = self._row_to_config(row)
                    all_map[row.server_id] = cfg
                    if row.is_enabled:
                        enabled_map[row.server_id] = cfg
        except Exception as exc:
            logger.warning("[mcp_service] Failed to load from DB: %s", exc)
            # Return stale cache if available
            if self._cache is not None:
                return dict(self._cache) if enabled_only else dict(self._cache_all or self._cache)
            return {}

        self._cache = enabled_map
        self._cache_all = all_map
        self._cache_ts = time.monotonic()

        return dict(enabled_map) if enabled_only else dict(all_map)

    def _row_to_config(self, row: AdminMcpServer) -> dict:
        """Convert a DB row to the config dict format."""
        cfg: Dict[str, Any] = {
            "transport": row.transport,
            "is_stable": row.is_stable,
        }

        # stdio fields
        if row.transport == "stdio":
            cfg["command"] = row.command or "python"
            cfg["args"] = list(row.args or [])

        # HTTP/SSE fields
        if row.transport in ("streamable_http", "sse"):
            cfg["url"] = _rewrite_builtin_mcp_host(row.url or "")

        # Build env: inherit from OS + explicit env_vars
        env = self._build_env(row)
        if env:
            cfg["env"] = env

        # Headers
        if row.headers:
            cfg["headers"] = dict(row.headers)

        return cfg

    def _build_env(self, row: AdminMcpServer) -> Dict[str, str]:
        """Merge env_inherit (from OS) + env_vars (from DB)."""
        env: Dict[str, str] = {}

        # Phase 1: inherit from OS environment
        for key in (row.env_inherit or []):
            val = os.getenv(key)
            if val is not None:
                env[key] = val

        # Phase 2: overlay admin-set explicit values
        for key, val in (row.env_vars or {}).items():
            if isinstance(val, str):
                env[key] = val

        # Phase 3: apply DB-driven overlays (model config, system config)
        try:
            from core.services.model_config import ModelConfigService
            overlay = ModelConfigService.get_instance().get_mcp_env_overlay()
            if overlay:
                env.update(overlay)
        except Exception:
            pass

        try:
            from core.services.system_config import SystemConfigService
            svc_overlay = SystemConfigService.get_instance().get_service_env_overlay()
            if svc_overlay:
                env.update(svc_overlay)
        except Exception:
            pass

        return env

    def get_owned_servers(self, user_id: str, enabled_only: bool = True) -> Dict[str, dict]:
        """Return the private MCPs a given user added themselves (owner_user_id == user_id).

        Queried from the DB on each call (the count is small), never cached globally, to avoid cross-user leakage. Only remote HTTP/SSE
        transports (the user entry point does not allow stdio).
        """
        if not user_id:
            return {}
        owned: Dict[str, dict] = {}
        try:
            with SessionLocal() as session:
                q = session.query(AdminMcpServer).filter(
                    AdminMcpServer.owner_user_id == user_id
                )
                if enabled_only:
                    q = q.filter(AdminMcpServer.is_enabled.is_(True))
                for row in q.order_by(AdminMcpServer.sort_order, AdminMcpServer.server_id).all():
                    owned[row.server_id] = self._row_to_config(row)
        except Exception as exc:
            logger.warning("[mcp_service] Failed to load owned servers for %s: %s", user_id, exc)
        return owned

    def get_server(self, server_id: str) -> Optional[dict]:
        """Get a single server config by ID."""
        servers = self.get_all_servers(enabled_only=False)
        return servers.get(server_id)

    def get_stable_server_ids(self) -> Set[str]:
        """Return set of server_ids where is_stable=True."""
        servers = self.get_all_servers(enabled_only=True)
        return {k for k, v in servers.items() if v.get("is_stable")}

    def invalidate_cache(self) -> None:
        """Clear cache so next call re-reads from DB."""
        with self._lock:
            self._cache = None
            self._cache_all = None
            self._cache_ts = 0.0

    def get_all_rows(self) -> List[AdminMcpServer]:
        """Return all DB rows (for admin API). Not cached."""
        with SessionLocal() as session:
            return session.query(AdminMcpServer).order_by(
                AdminMcpServer.sort_order, AdminMcpServer.server_id
            ).all()


# ── Built-in MCP catalog seed (single source of truth for fresh installs) ──────
#
# On a Docker/compose deployment these rows are inserted by alembic seed
# migrations. On the no-Docker local/quick-install profile the DB is created via
# ``Base.metadata.create_all()`` (alembic never runs — its seed SQL is Postgres
# only), so the built-in catalog would be empty. ``seed_builtin_mcp_servers_if_empty``
# closes that gap: it seeds this canonical set **only when no global built-in row
# exists yet**, so it is a one-time bootstrap that never resurrects rows an admin
# has since deleted, and it is a no-op on any DB that already has the catalog.
#
# The URL is templated per-server from ``mcp_servers._ports`` + ``settings.server.
# mcp_host`` at seed time, so compose gets ``http://mcp:<port>/mcp/`` and local
# gets ``http://127.0.0.1:<port>/mcp/`` from the same definition. Only the 8
# built-in servers the ``mcp`` container / ``_launcher`` actually serves are
# included; plugin-provided servers (automation_task / skill_manager /
# site_publish) arrive via plugin install, and external-service ones (db_query /
# es_query) need their own containers, so neither belongs in the base seed.
BUILTIN_MCP_SERVERS: List[Dict[str, Any]] = [
    {
        "server_id": "query_database",
        "display_name": "数据库查询",
        "description": "查询数据仓库中的行业指标与统计数值，支持自然语言提问直接获取精确数据。",
        "user_intro": (
            "## 用途\n用自然语言向结构化数据仓库提问，自动生成查询语句并返回精确数值，"
            "避免互联网信息的不确定性。\n\n## 适用场景\n- \"某年某地区的工业增加值是多少\"\n"
            "- \"各区今年 1–9 月固投同比\"\n\n## 输出示例\n- 精确数值 + 数据口径标注\n"
            "- 时间 / 地区 / 行业等多维度数据切片\n- 同比、环比、累计值自动计算\n"
        ),
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 0,
        "icon": "/home/mcp/knowledge.svg",
    },
    {
        "server_id": "retrieve_dataset_content",
        "display_name": "知识库检索",
        "description": "从公有/私有知识库中语义检索政策文件、产业报告及用户上传文档，支持混合检索与重排序。",
        "user_intro": None,
        "is_stable": False,
        "is_enabled": True,
        "sort_order": 1,
        "icon": "/home/mcp/learning.svg",
    },
    {
        "server_id": "internet_search",
        "display_name": "互联网搜索",
        "description": "通过互联网实时搜索公开网页、新闻及财经资讯，作为数据库与知识库之外的信息兜底。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 2,
        "icon": "/home/mcp/internet.svg",
    },
    {
        "server_id": "ai_chain_information_mcp",
        "display_name": "产业知识中心查询",
        "description": "获取产业链全景分析报告、核心数据指标、产业动态资讯、AI 领域热点聚合及企业画像查询。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 3,
        "icon": "/home/mcp/industry-chain.svg",
    },
    {
        "server_id": "generate_chart_tool",
        "display_name": "数据可视化",
        "description": "根据给定数据调用 Python 生成柱状图、折线图、饼图等可视化图表，结果以图片形式直接展示。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 4,
        "icon": "/home/mcp/data.svg",
    },
    {
        "server_id": "report_export_mcp",
        "display_name": "报告导出",
        "description": "将 Markdown 格式的分析报告导出为 Word 文档，或将表格数据导出为 Excel 文件供下载。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": False,
        "sort_order": 5,
        "icon": "/home/mcp/report.svg",
    },
    {
        "server_id": "web_fetch",
        "display_name": "网站信息抓取",
        "description": "抓取指定网页 URL 的内容，提取正文文本或 Markdown，支持搜索引擎结果页解析。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 6,
        "icon": "/home/mcp/source.svg",
    },
    {
        "server_id": "batch_runner",
        "display_name": "批量执行",
        "description": (
            "对一组对象（Excel 行/多份文档/文本枚举）批量执行同一个任务；先生成可确认的计划，"
            "用户审阅模板后再逐条执行。"
        ),
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 100,
        "icon": "/home/mcp/list.svg",
    },
]


def is_removed_builtin_mcp_server(
    server_id: str,
    *,
    source_plugin: Optional[str] = None,
) -> bool:
    """Return whether a legacy built-in row is unavailable in this edition.

    CE removes commercial MCP packages and their port registrations at build
    time, while an upgraded deployment may still retain the old global rows in
    ``admin_mcp_servers``.  Those stale rows must not be connected or exposed as
    working runtime tools.  Plugin-provided rows remain valid because their
    lifecycle and process registration are managed by the plugin itself.
    """
    if source_plugin is not None:
        return False

    from mcp_servers._ports import PORTS

    builtin_ids = {str(spec["server_id"]) for spec in BUILTIN_MCP_SERVERS}
    return server_id in builtin_ids and server_id not in PORTS


def seed_builtin_mcp_servers_if_empty(db) -> List[str]:
    """Seed the built-in global MCP catalog when it is entirely absent.

    Returns the list of seeded server_ids (empty if the catalog already existed).
    Idempotent and safe on every deployment: it only fires when **no** global
    built-in row (``owner_user_id IS NULL AND source_plugin IS NULL``) exists, so
    it bootstraps a fresh ``create_all`` DB (local profile) without touching a DB
    that alembic already seeded and without resurrecting admin-deleted rows.
    """
    from mcp_servers._ports import PORTS
    from core.config.settings import settings

    existing = (
        db.query(AdminMcpServer.server_id)
        .filter(
            AdminMcpServer.owner_user_id.is_(None),
            AdminMcpServer.source_plugin.is_(None),
        )
        .first()
    )
    if existing is not None:
        return []

    host = settings.server.mcp_host
    seeded: List[str] = []
    for spec in BUILTIN_MCP_SERVERS:
        sid = spec["server_id"]
        port = PORTS.get(sid)
        if port is None:
            continue
        db.add(
            AdminMcpServer(
                server_id=sid,
                display_name=spec["display_name"],
                description=spec.get("description", ""),
                user_intro=spec.get("user_intro"),
                transport="streamable_http",
                url=f"http://{host}:{port}/mcp/",
                is_stable=spec.get("is_stable", True),
                is_enabled=spec.get("is_enabled", True),
                sort_order=spec.get("sort_order", 0),
                icon=spec.get("icon"),
            )
        )
        seeded.append(sid)
    if seeded:
        db.commit()
    return seeded
