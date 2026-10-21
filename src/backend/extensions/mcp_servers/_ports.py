"""Deployment-adjusted ports for registry-managed MCP processes.

Base ports and installation placement live in extensions.mcp_servers.registry.
Backend connection configuration and the process launcher consume this mapping.
Deployed port assignments remain stable.
"""

from __future__ import annotations

import os


def _parse_port_offset(raw: str | None, *, highest_base_port: int = 9117) -> int:
    """Parse the optional local-desktop MCP port namespace.

    Compose and ordinary server deployments leave the value unset and retain
    the stable 9100-range contract. Branded desktop shells can supply an offset
    so two products may run their private MCP meshes on the same computer.
    """

    value = (raw or "0").strip()
    try:
        offset = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"HUGAGENT_LOCAL_MCP_PORT_OFFSET 必须是整数，当前为 {value!r}"
        ) from exc
    if offset < 0 or highest_base_port + offset > 65535:
        raise RuntimeError(
            "HUGAGENT_LOCAL_MCP_PORT_OFFSET 超出有效端口范围："
            f"{offset}"
        )
    return offset


_PORT_OFFSET = _parse_port_offset(os.getenv("HUGAGENT_LOCAL_MCP_PORT_OFFSET"))

# Installation placement and base ports share one registry.
from extensions.mcp_servers.registry import SERVERS

_BASE_PORTS = {key: server.port for key, server in SERVERS.items()
               if server.exposure == "process" and server.port is not None}

PORTS: dict[str, int] = {
    server_id: port + _PORT_OFFSET for server_id, port in _BASE_PORTS.items()
}


def package_name(server_id: str) -> str:
    """Return the python package directory name for a given server_id.

    Most server_ids are also the package name; a handful that already end
    in ``_mcp`` use it verbatim. The launcher needs the package name to
    spawn ``python -m extensions.mcp_servers.<pkg>.server``.
    """
    server = SERVERS.get(server_id)
    return server.package if server else (server_id if server_id.endswith("_mcp") else f"{server_id}_mcp")
