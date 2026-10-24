"""Capture real tool schemas for MCP servers that only know their tools by name.

A plugin manifest declares its tools for display — ``{name, description}`` with no
parameters. Probing the running server is what fills ``AdminMcpServer.tools_json``
with each tool's ``inputSchema``, and that column is the desktop's only source of
tool parameters: the cloud ships those entries verbatim in the capability manifest
and the desktop builds the model-facing tool from them. A server left with
display-only entries therefore offers the model parameter-less tools, so arguments
have nowhere to go and every call arrives blank.

Backfill runs where the servers are reachable (cloud startup, and after a plugin
install or upgrade rewrites the manifest), never on the per-message path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Probing is a network round trip per server; keep startup bounded while still
# repairing a whole installation in one pass.
_MAX_CONCURRENCY = 4
_PROBE_TIMEOUT_S = 20.0


def _needs_backfill(tools_json) -> bool:
    from core.capabilities.mcp.tool_schema import has_usable_schema

    if not isinstance(tools_json, list) or not tools_json:
        return False
    return any(not has_usable_schema(item) for item in tools_json)


async def backfill_missing_tool_schemas(server_ids: Optional[List[str]] = None) -> int:
    """Probe enabled MCP servers whose stored tools carry no schema.

    Returns the number of servers whose ``tools_json`` was refreshed. Failures are
    logged and skipped: an unreachable server keeps its display-only entries, and
    the manifest client withholds those tools rather than offering broken ones.
    """
    from core.capabilities.mcp.management import probe_mcp_connectivity
    from core.db.threads import run_in_session

    from core.harness.registry import device_catalog

    from core.harness.registry.run_capabilities import has_run_capabilities
    if has_run_capabilities() and not server_ids:
        from core.harness.registry.local_publication import migrate_local_plugins as migrate_plugins
        from core.harness.registry.local_publication import migrate_local_skills as migrate_skills
        await asyncio.to_thread(migrate_plugins)
        await asyncio.to_thread(migrate_skills)

    if device_catalog.active() and not server_ids:
        # 混合模式下工具 schema 随云端能力清单下发，不靠探活取。业务库里留下的
        # 本机插件行指向 compose 服务名，桌面端根本连不上——照探只会每次启动都
        # 刷一轮「无法连接远端 MCP」，修不好任何东西。
        logger.debug("[mcp-schema] hybrid desktop: schemas come from the cloud manifest, skipping probe")
        return 0

    rows = await run_in_session(_servers_to_backfill, server_ids)
    if not rows:
        return 0

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    repaired: Dict[str, list] = {}

    async def _probe(row) -> None:
        async with semaphore:
            try:
                ok, detail = await probe_mcp_connectivity(
                    row, validate=True, timeout_seconds=_PROBE_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 - one server must not stop the pass
                logger.warning(
                    "[mcp-schema] probe raised for '%s': %s", row.server_id, exc
                )
                return
            if ok and not _needs_backfill(row.tools_json):
                repaired[row.server_id] = row.tools_json
            else:
                logger.warning(
                    "[mcp-schema] '%s' still has tools without a schema: %s",
                    row.server_id,
                    detail,
                )

    await asyncio.gather(*(_probe(row) for row in rows))
    if repaired:
        await run_in_session(_store_schemas, repaired)
        logger.info(
            "[mcp-schema] tool schemas captured for %d server(s): %s",
            len(repaired),
            ", ".join(sorted(repaired)),
        )
    return len(repaired)


def _servers_to_backfill(db, server_ids):
    """Enabled servers whose tools lack schemas, detached for probing."""
    from core.capabilities.plugins.management.bundle_upgrade import refresh
    from core.db.models import AdminMcpServer

    refresh(db)
    query = db.query(AdminMcpServer).filter(AdminMcpServer.is_enabled.is_(True))
    if server_ids:
        query = query.filter(AdminMcpServer.server_id.in_(list(server_ids)))
    rows = [row for row in query.all() if _needs_backfill(row.tools_json)]
    db.expunge_all()
    return rows


def _store_schemas(db, repaired) -> None:
    from core.db.models import AdminMcpServer

    for row in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(list(repaired))):
        row.tools_json = repaired[row.server_id]
    db.commit()
