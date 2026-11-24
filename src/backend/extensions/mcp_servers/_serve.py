"""Shared launcher for MCPServer servers.

Every MCP server's ``main()`` calls ``run(mcp, default_port=NNNN)``.
Picks transport from ``--transport`` (``stdio`` for local debug, the
default; ``streamable-http`` for the dedicated mcp container).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def _streamable_http_bind_host() -> str:
    return os.getenv("MCP_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"


def transport_security():
    """DNS-rebinding guard shared by every built-in streamable-HTTP server."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "mcp:*", "hugagent-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )


def run(mcp: "MCPServer", default_port: int) -> None:
    parser = argparse.ArgumentParser(description=mcp.name)
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        host = _streamable_http_bind_host()
        mcp.run("streamable-http", host=host, port=args.port, transport_security=transport_security())
    else:
        asyncio.run(mcp.run_stdio_async())
