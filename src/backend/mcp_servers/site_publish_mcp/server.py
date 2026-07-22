#!/usr/bin/env python3
"""Community-edition site publishing MCP server."""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp_servers.site_publish_mcp import impl

mcp = FastMCP("hugagent-site-publish")

_HDR_USER = "x-current-user-id"
_HDR_CHAT = "x-chat-id"
_HDR_CONV = "x-conversation-id"


def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        value = ctx.request_context.request.headers.get(name)
        return value or None
    except Exception:
        return None


@mcp.tool()
async def publish_site(
    title: str,
    src_dir: str = "",
    source_dir: str = "",
    slug: str = "",
    site_id: str = "",
    visibility: str = "public",
    description: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Publish a site from the current sandbox and return its hosted URL.

    ``visibility`` accepts ``public`` or ``private``. Static sites may omit
    ``src_dir`` in a project chat. Build-based sites pass the build output as
    ``src_dir`` and the editable source folder as ``source_dir``.
    """
    return await impl.publish_site(
        user_id=_hdr(ctx, _HDR_USER) or "",
        chat_id=_hdr(ctx, _HDR_CHAT) or _hdr(ctx, _HDR_CONV) or "",
        src_dir=src_dir,
        source_dir=source_dir,
        title=title,
        slug=slug,
        site_id=site_id,
        visibility=visibility,
        description=description,
    )


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9113)


if __name__ == "__main__":
    main()
