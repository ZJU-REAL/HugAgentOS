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


def builtin_launcher_serves_this_plane() -> bool:
    """内置 MCP 是否由本执行面自己承载。

    容器部署由专用 mcp 容器承载；本机单模式由本进程拉起的 launcher 承载；桌面双模式
    （桥接密钥已注入）工具全部来自云端 manifest，本机不运行任何内置 MCP。
    """
    from core.config.settings import settings

    if not settings.deploy.is_local:
        return True
    from core.auth.desktop_bridge import bridge_enabled

    return not bridge_enabled()


def launcher_backed(url: str) -> bool:
    """该 HTTP 地址是否指向本机 launcher 的端口段（loopback + `_ports` 登记的端口）。"""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    try:
        return parsed.port in set(_PORTS.values())
    except ValueError:
        return False
