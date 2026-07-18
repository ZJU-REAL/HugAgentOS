"""MCP client pool manager for AgentScope 2.0.

Manages MCPClient instances with TTL caching.

Migration notes (1.x → 2.0)
---------------------------
- ``StdIOStatefulClient(name, command, args, env)`` →
  ``MCPClient(name=, is_stateful=True, mcp_config=StdioMCPConfig(command, args, env))``.
- In 2.0 ``Toolkit`` is constructed **once** (``Toolkit(tools=, mcps=, ...)``); there are
  no incremental ``register_mcp_client`` / ``register_tool_function`` methods and no
  ``namesake_strategy``. Stateful clients must ``connect()`` **before** being passed
  into ``Toolkit``.
- Therefore ``connect_mcp_clients`` only connects and returns the client list; the
  Toolkit is built once in agent_factory via
  ``Toolkit(tools=[...FunctionTool...], mcps=clients)``.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List

from pydantic import ConfigDict

from agentscope.mcp import MCPClient, StdioMCPConfig
from agentscope.tool import MCPTool

logger = logging.getLogger(__name__)


def _cyfunc_probe() -> None:  # after Cython compilation its type is cython_function_or_method
    pass


# Cython compiles methods into cython_function_or_method; pydantic v2 does not recognize
# it as a method and treats it as an "un-annotated field", raising PydanticUserError,
# which makes the whole module fail to import after hardened compilation and fall back to
# plaintext. Registering that type in ignored_types makes pydantic ignore compiled
# methods; under pure Python it is just FunctionType, so there is no side effect.
_CYFUNCTION_TYPE = type(_cyfunc_probe)

# TTL for the instance-level memoization of ``list_tools``. Within a single turn,
# agent_factory's HTTP MCP liveness probe (``_connect_http``, parallel list_tools) and
# the subsequent ``Toolkit.get_tool_schemas()`` (AgentScope ``_get_available_tools``
# does a **serial** ``await list_tools()`` per client) each enumerate tools once per
# server — stateless HTTP creates a new connection every time, and the serial
# re-enumeration is the main cause of the ~2s agent build time (11 servers, 5 slow
# search MCPs at ~450ms each, chained serially). Memoization cuts the second
# enumeration to ~0ms (the probe pass stays parallel at ~465ms). Per-request HTTP
# clients get a fresh instance every turn → cache lifetime = one turn, no header
# leakage across turns/users; pooled stdio clients survive across turns, so the TTL
# is the safety net (tool definitions are static anyway; config changes rebuild the
# pool → new instances).
_LIST_TOOLS_TTL_S = 300.0


class BareNameMCPClient(MCPClient):
    """Restore the server-side bare name of MCP tools (``internet_search`` rather than ``mcp__internet_search__internet_search``).

    AgentScope 2.0's ``MCPTool`` adapter rewrites the outward-facing name to
    ``mcp__<server>__<tool>``, but this project's display-name mapping
    (core/config/display_names), citation extraction (orchestration/citations
    dispatches on bare names like ``internet_search``), catalog gating, tool
    references in system prompts and SKILL.md, and frontend icons/panels/renderers
    are all built on the 1.x bare names. ``MCPTool.__call__`` actually calls the
    server via ``self._tool.name`` (the bare name), so rewriting the adapter's
    ``.name`` only affects the LLM-visible name and the SSE event stream; the call
    path is unaffected.
    """

    model_config = ConfigDict(ignored_types=(_CYFUNCTION_TYPE,))

    async def get_tool(self, name: str) -> MCPTool:
        tool = await super().get_tool(name)
        tool.name = tool._tool.name
        return tool

    async def list_tools(self) -> List[MCPTool]:
        """Instance-level TTL memoization to eliminate duplicate list_tools for the same server within a turn.

        See the ``_LIST_TOOLS_TTL_S`` comment at the top of the module: the liveness
        probe and ``get_tool_schemas`` each call once, and the second serial
        re-enumeration is the main cause of the ~2s agent build time. The cache hangs
        off the instance (``__slots__`` includes ``__dict__``, so arbitrary attributes
        can be set); a new client per turn → naturally scoped to a single turn.
        """
        cached = getattr(self, "_lt_cache", None)
        if cached is not None:
            expires_at, tools = cached
            if time.monotonic() < expires_at:
                return tools
        tools = await super().list_tools()
        self._lt_cache = (time.monotonic() + _LIST_TOOLS_TTL_S, tools)
        return tools


def make_stdio_client(server_name: str, server_cfg: dict) -> MCPClient:
    """Build a stdio MCPClient (not yet connected) from a server config in configs/mcp_config.py."""
    return BareNameMCPClient(
        name=server_name,
        is_stateful=True,
        mcp_config=StdioMCPConfig(
            command=server_cfg.get("command", "python"),
            args=server_cfg.get("args", []),
            env=server_cfg.get("env") or None,
        ),
    )


async def connect_mcp_clients(
    mcp_servers: Dict[str, dict],
) -> List[MCPClient]:
    """Connect to MCP servers and return the connected MCPClient list."""
    clients: List[MCPClient] = []
    for server_name, server_cfg in mcp_servers.items():
        client = make_stdio_client(server_name, server_cfg)
        try:
            await client.connect()
            clients.append(client)
            logger.debug("MCP client '%s' connected", server_name)
        except Exception as exc:
            logger.warning("Failed to connect MCP server '%s': %s", server_name, exc)
            try:
                await client.close()
            except Exception:
                pass
    return clients


async def close_clients(clients: List[MCPClient]) -> None:
    """Safely close a list of MCP clients."""
    for client in clients:
        try:
            await client.close()
        except Exception as exc:
            logger.debug("Error closing MCP client: %s", exc)
