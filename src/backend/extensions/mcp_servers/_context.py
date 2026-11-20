"""Per-call identity read from the runtime headers the backend injects into every MCP request."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import Context

HDR_USER = "x-current-user-id"


def header(ctx: Optional[Context], name: str) -> Optional[str]:
    """Return a request header, or None outside an HTTP request or when it is empty."""
    if ctx is None:
        return None
    try:
        return ctx.request_context.request.headers.get(name) or None
    except Exception:
        return None


def current_user(ctx: Optional[Context]) -> str:
    return header(ctx, HDR_USER) or ""
