"""Shared launcher for FastMCP servers.

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
    from mcp.server.fastmcp import FastMCP


def _streamable_http_bind_host() -> str:
    return os.getenv("MCP_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"


def run(mcp: "FastMCP", default_port: int) -> None:
    parser = argparse.ArgumentParser(description=mcp.name)
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.settings.port = args.port
        # Compose needs all-interface binding inside its private container
        # network.  The no-Docker local/desktop launcher explicitly supplies
        # MCP_BIND_HOST=127.0.0.1 so these internal control-plane ports are not
        # exposed to the user's LAN (and do not trigger an avoidable Windows
        # firewall prompt).
        mcp.settings.host = _streamable_http_bind_host()
        # MCP's default DNS-rebinding allow-list is localhost only. Backend
        # reaches us via the docker DNS name (e.g. ``mcp:9108``); rebinding
        # protection isn't relevant on a private network with no browser
        # traffic.
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        mcp.run("streamable-http")
    else:
        asyncio.run(mcp.run_stdio_async())
