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
from typing import List, Optional

logger = logging.getLogger(__name__)

# Probing is a network round trip per server; keep startup bounded while still
# repairing a whole installation in one pass.
_MAX_CONCURRENCY = 4
_PROBE_TIMEOUT_S = 20.0


def _needs_backfill(tools_json) -> bool:
    from core.llm.mcp_manager import has_usable_schema

    if not isinstance(tools_json, list) or not tools_json:
        return False
    return any(not has_usable_schema(item) for item in tools_json)


async def backfill_missing_tool_schemas(server_ids: Optional[List[str]] = None) -> int:
    """Probe enabled MCP servers whose stored tools carry no schema.

    Returns the number of servers whose ``tools_json`` was refreshed. Failures are
    logged and skipped: an unreachable server keeps its display-only entries, and
    the manifest client withholds those tools rather than offering broken ones.
    """
    from core.db.engine import SessionLocal
    from core.db.models import AdminMcpServer
    from core.services.mcp_management_service import probe_mcp_connectivity

    db = SessionLocal()
    try:
        query = db.query(AdminMcpServer).filter(AdminMcpServer.is_enabled.is_(True))
        if server_ids:
            query = query.filter(AdminMcpServer.server_id.in_(list(server_ids)))
        rows = [row for row in query.all() if _needs_backfill(row.tools_json)]
        if not rows:
            return 0

        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        repaired: List[str] = []

        async def _probe(row) -> None:
            async with semaphore:
                try:
                    ok, detail = await probe_mcp_connectivity(
                        row, db, timeout_seconds=_PROBE_TIMEOUT_S
                    )
                except Exception as exc:  # noqa: BLE001 - one server must not stop the pass
                    logger.warning(
                        "[mcp-schema] probe raised for '%s': %s", row.server_id, exc
                    )
                    return
                if ok and not _needs_backfill(row.tools_json):
                    repaired.append(row.server_id)
                else:
                    logger.warning(
                        "[mcp-schema] '%s' still has tools without a schema: %s",
                        row.server_id,
                        detail,
                    )

        await asyncio.gather(*(_probe(row) for row in rows))
        if repaired:
            db.commit()
            logger.info(
                "[mcp-schema] tool schemas captured for %d server(s): %s",
                len(repaired),
                ", ".join(sorted(repaired)),
            )
        else:
            db.rollback()
        return len(repaired)
    finally:
        db.close()
