"""Shared launcher for FastMCP servers.

Every MCP server's ``main()`` calls ``run(mcp, default_port=NNNN)``.
Picks transport from ``--transport`` (``stdio`` for local debug, the
default; ``streamable-http`` for the dedicated mcp container).
"""
from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


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
        mcp.settings.host = "0.0.0.0"  # noqa: S104 — private docker network
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
