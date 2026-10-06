"""Agent assembly: mcp config. """

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import core.config.catalog as catalog
from core.config.catalog_loader import DB_HIDDEN_SERVERS, DB_UMBRELLA_ID
from core.llm.mcp_pool import MCPConnectionPool
from core.services.mcp_service import McpServerConfigService
from orchestration.registry import AgentSpec


def _effective_mcp_server_keys(
    cfg,
    agent_spec: Optional[AgentSpec],
    enabled_mcp_ids: Optional[list[str]] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    owned_servers: Optional[dict] = None,
    bridge_servers: Optional[dict] = None,
) -> list[str]:
    all_servers = dict(
        McpServerConfigService.get_instance().get_all_servers(enabled_only=enabled_mcp_ids is None)
    )
    # Config-source precedence (later update wins on same server_id):
    #   global rows < bridge (desktop cloud gateway; cloud is the source of
    #   truth for a capability it takes over) < owned (a user's own private
    #   MCP is the narrowest, most explicit grant).
    if bridge_servers:
        all_servers.update(bridge_servers)
    # Merge in the current user's self-added private MCPs (owner-isolated; already filtered by user_id at the service layer)
    if owned_servers:
        all_servers.update(owned_servers)
    all_keys = list(all_servers.keys())
    # Include the "database query" umbrella id in the gating set so it survives
    # the runtime/catalog/spec intersection filters (it isn't a real server, so
    # at the end it is expanded into the real DB servers and then discarded).
    allow: Set[str] = set(all_keys) | {DB_UMBRELLA_ID}

    # NOTE: Prompt config mcp_servers.enabled whitelist is intentionally
    # skipped here. All MCP servers are now DB-managed via admin panel,
    # and the catalog + user override + runtime filters provide sufficient
    # gating. The legacy prompt config whitelist would block newly added
    # admin MCP servers that aren't in the static config.

    if isinstance(enabled_mcp_ids, list):
        runtime_set = set([x for x in enabled_mcp_ids if isinstance(x, str) and x.strip()])
        allow &= runtime_set
    else:
        catalog_set = set(catalog.get_enabled_ids("mcp"))
        allow &= catalog_set

    if agent_spec is not None:
        spec_enabled = getattr(getattr(agent_spec, "mcp_servers", None), "enabled", None) or []
        if spec_enabled:
            spec_set = set([x for x in spec_enabled if isinstance(x, str) and x.strip()])
            allow &= spec_set

    # Note: empty enabled_kb_ids [] means no KBs selected in frontend (e.g. catalog
    # KB list was empty because an external provider was unreachable). We do NOT remove the tool
    # in this case — the MCP impl will auto-resolve available KBs at call time.

    # "Database query" umbrella expansion: when the user/catalog selects the
    # single database_query, allow the actually enabled DB servers under it
    # (query_database / db_query / es_query; apply switches is_enabled by data
    # source type).
    if DB_UMBRELLA_ID in allow:
        allow |= {k for k in all_keys if k in DB_HIDDEN_SERVERS}
    allow.discard(DB_UMBRELLA_ID)

    return [k for k in all_keys if k in allow]


def _filter_mcp_servers_by_keys(
    enabled_keys: list[str],
    owned_servers: Optional[dict] = None,
    bridge_servers: Optional[dict] = None,
) -> dict:
    enabled_set = set(enabled_keys)
    all_servers = dict(McpServerConfigService.get_instance().get_all_servers(enabled_only=False))
    # Same precedence as _effective_mcp_server_keys: global < bridge < owned.
    if bridge_servers:
        all_servers.update(bridge_servers)
    if owned_servers:
        all_servers.update(owned_servers)
    return {k: v for k, v in all_servers.items() if k in enabled_set}


def _required_mcp_server_keys(
    required_mcp_ids: List[str], enabled_mcp_keys: List[str]
) -> List[str]:
    """Resolve user-facing connector IDs to the concrete connected servers."""
    enabled = set(enabled_mcp_keys)
    resolved: List[str] = []
    for connector_id in required_mcp_ids:
        if connector_id == DB_UMBRELLA_ID:
            candidates = [key for key in enabled_mcp_keys if key in DB_HIDDEN_SERVERS]
        else:
            candidates = [connector_id] if connector_id in enabled else []
        for key in candidates:
            if key not in resolved:
                resolved.append(key)
    return resolved


def _inject_runtime_headers(
    enabled_servers: dict,
    *,
    current_user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
    reranker_enabled: bool = False,
) -> dict:
    """Inject the "per-request runtime context" as HTTP headers into ALL enabled MCP servers — no special-casing by server name.

    Each MCP server takes what it needs: KB reads X-Allowed-*/X-Reranker-Enabled,
    scheduled tasks read X-Channel-*/X-Conversation-*, and any server can read
    X-Current-User-Id. New MCP plugins get the context without modifying this
    file ("treated as an ordinary plugin"). streamable_http/sse use headers;
    stdio (a few runtime plugins/legacy paths) falls back to equivalent env
    variables. Injecting into every server is safe — servers that don't care
    simply ignore unknown headers.
    """
    if not enabled_servers:
        return enabled_servers

    from core.kb.external_provider import runtime_request_context

    normalized = [str(x).strip() for x in (enabled_kb_ids or []) if str(x).strip()]
    local_ids = [x for x in normalized if x.startswith("kb_")]
    external_headers, external_env = runtime_request_context(normalized)
    origin = channel_origin or {}

    ctx_headers = {
        "X-Current-User-Id": current_user_id or "",
        # X-Chat-Id = this chat's id (for web main conversations it is also the
        # sandbox session key). MCPs that need to reach the user's sandbox
        # (site_publish etc.) use it to locate the session; X-Conversation-Id
        # only has a value on external channels (DingTalk etc.).
        "X-Chat-Id": chat_id or "",
        "X-Channel-Id": origin.get("channel_id") or "",
        "X-Conversation-Id": origin.get("conversation_id") or "",
        "X-Allowed-Kb-Ids": ",".join(local_ids),
        "X-Reranker-Enabled": "true" if reranker_enabled else "false",
    }
    ctx_headers.update(external_headers)
    ctx_env = {
        "CURRENT_USER_ID": current_user_id or "",
        "CURRENT_CHAT_ID": chat_id or "",
        "LOCAL_KB_ALLOWED_IDS": ",".join(local_ids),
        "RERANKER_ENABLED": "true" if reranker_enabled else "false",
    }
    ctx_env.update(external_env)

    out: dict = {}
    for key, cfg in enabled_servers.items():
        if not isinstance(cfg, dict):
            out[key] = cfg
            continue
        c = dict(cfg)
        is_http = bool(c.get("url")) or c.get("transport") in ("streamable_http", "sse")
        if is_http:
            headers = dict(c.get("headers") or {})
            headers.update(ctx_headers)
            from core.llm.mcp_invocation import for_url
            headers.update(for_url(c.get("url"), current_user_id, chat_id))
            c["headers"] = headers
        else:
            env_cfg = dict(c.get("env") or {})
            env_cfg.update(ctx_env)
            c["env"] = env_cfg
        out[key] = c
    return out


async def warmup_mcp_tools() -> None:
    """Initialize the MCP connection pool at startup.

    Reads MCP server configs from DB (via McpServerConfigService) and
    connects to all stable servers. Per-request servers (e.g.
    retrieve_dataset_content) are spawned on demand.
    """
    import logging
    import time

    log = logging.getLogger(__name__)

    # DB overlays (model config, system config) are already applied inside
    # McpServerConfigService._build_env(), so no manual overlay needed here.
    svc = McpServerConfigService.get_instance()
    servers = svc.get_all_servers(enabled_only=True)

    if not servers:
        log.info("[warmup] No MCP servers configured – skipping warmup")
        return

    log.info("[warmup] Initializing MCP connection pool for %d server(s)…", len(servers))
    start = time.monotonic()

    try:
        pool = MCPConnectionPool.get_instance()
        await pool.initialize(servers)
        elapsed = time.monotonic() - start
        log.info(
            "[warmup] MCP pool initialized: %d stable connections in %.2fs",
            pool.stable_client_count,
            elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        log.warning("[warmup] MCP pool initialization failed after %.2fs: %s", elapsed, exc)
