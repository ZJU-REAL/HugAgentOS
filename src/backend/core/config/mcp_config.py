"""MCP server configuration for HugAgentOS.

Every MCP server runs as a long-running streamable-http process inside the
dedicated ``mcp`` Docker container. ``backend`` connects via
``HttpStatefulClient`` (see ``core/llm/agent_factory.py``).

Server IDs are also keys in ``configs/display_names.py`` and consumed by
``configs/catalog_loader.py``; renaming requires updating those.
"""

from __future__ import annotations

from typing import Dict

from core.config.display_names import (  # noqa: F401
    MCP_SERVER_DESCRIPTIONS,
    MCP_SERVER_DISPLAY_NAMES,
    TOOL_DISPLAY_NAMES,
)
from mcp_servers._ports import PORTS as _PORTS


def _mcp_http_url(server_id: str) -> str:
    from core.config.settings import settings

    return f"http://{settings.server.mcp_host}:{_PORTS[server_id]}/mcp/"


MCP_SERVERS: Dict[str, dict] = {
    server_id: {
        "transport": "streamable_http",
        "url": _mcp_http_url(server_id),
        "env": {},
    }
    for server_id in _PORTS
}
