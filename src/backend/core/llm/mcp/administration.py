"""Refresh live MCP connections after configuration changes."""
import logging
logger = logging.getLogger(__name__)

async def reinitialize_mcp_pool(source: str = "service_configs") -> None:
    """Invalidate MCP config cache and reinitialize stable connections.

    Called after system config changes so that MCP sub-processes pick up
    new env vars (e.g. updated API URLs/keys).
    """
    try:
        from core.capabilities.mcp.service import McpServerConfigService

        mcp_svc = McpServerConfigService.get_instance()
        mcp_svc.invalidate_cache()

        from core.llm.mcp.pool import MCPConnectionPool

        pool = MCPConnectionPool.get_instance()
        if pool.is_initialized:
            new_configs = mcp_svc.get_all_servers()
            await pool.reinitialize_if_config_changed(new_configs)
            logger.info("[%s] MCP pool reinitialize triggered after config update", source)
    except Exception as exc:
        logger.warning("[%s] MCP pool reinitialize failed: %s", source, exc)
