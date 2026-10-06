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

import asyncio
import logging
import time
from contextvars import ContextVar
from functools import wraps
from typing import Any, Dict, List

import httpx
import mcp.types
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ConfigDict, Field

from agentscope.mcp import MCPClient, StdioMCPConfig
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import MCPTool, ToolBase, ToolChunk

logger = logging.getLogger(__name__)


def has_usable_schema(tool: Any) -> bool:
    """Whether a manifest tool entry carries the server's real ``inputSchema``.

    A tool the platform only knows by name and description (a plugin manifest
    lists its tools for display, without schemas) must not be handed to a model:
    the parameter object would be empty, so the model has nowhere to put its
    arguments and every call arrives blank.

    The marker is whether a schema was captured at all, not whether it declares
    parameters: a server is free to describe an argument-less tool as
    ``{"type": "object"}``, while an entry that never reached the server carries
    no ``inputSchema`` key, which normalizes to an empty dict.
    """
    if not isinstance(tool, dict):
        return False
    schema = tool.get("inputSchema") or tool.get("input_schema")
    return isinstance(schema, dict) and bool(schema)


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
    (core/config/display_names), citation extraction (orchestration/citation_anchor
    dispatches on bare names like ``internet_search``), catalog gating, tool
    references in system prompts and SKILL.md, and frontend icons/panels/renderers
    are all built on the 1.x bare names. ``MCPTool.__call__`` actually calls the
    server via ``self._tool.name`` (the bare name), so rewriting the adapter's
    ``.name`` only affects the LLM-visible name and the SSE event stream; the call
    path is unaffected.
    """

    model_config = ConfigDict(ignored_types=(_CYFUNCTION_TYPE,), arbitrary_types_allowed=True)
    oauth_provider: Any = Field(default=None, exclude=True)

    def _create_http_client(self):
        """Attach the SDK OAuth provider without forking AgentScope's client."""
        from core.llm.mcp_invocation import HEADER, for_url
        config = self.mcp_config
        signed = any(key.lower() == HEADER.lower() for key in (config.headers or {}))
        if self.oauth_provider is None and not signed:
            return super()._create_http_client()
        oauth_hook = getattr(self.oauth_provider, "mcp_request_hook", None)
        async def request_hook(request):
            if oauth_hook:
                await oauth_hook(request)
            if signed:
                request.headers.update(for_url(config.url, request.headers.get("x-current-user-id"), request.headers.get("x-chat-id")))
        event_hooks = {"request": [request_hook]}
        if config.url.endswith("/sse") or config.url.endswith("/messages/"):
            def _http_client_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(
                    headers=headers,
                    timeout=timeout,
                    auth=auth,
                    event_hooks=event_hooks,
                )

            return sse_client(
                url=config.url,
                headers=config.headers,
                timeout=config.timeout,
                auth=self.oauth_provider,
                httpx_client_factory=_http_client_factory,
            )
        http_client = httpx.AsyncClient(
            headers=config.headers,
            timeout=config.timeout,
            auth=self.oauth_provider,
            event_hooks=event_hooks,
        )
        return streamable_http_client(url=config.url, http_client=http_client)

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


from core.llm.gateway_mcp_tool import GatewayMCPTool


class ManifestMCPClient(BareNameMCPClient):
    """Network-free MCP discovery backed only by the current cloud manifest."""

    manifest_tools: List[Dict[str, Any]] = Field(default_factory=list, exclude=True)
    gateway_invoke_url: str = Field(exclude=True)
    schema_hash: str = Field(exclude=True)
    gateway_transport: Any = Field(default=None, exclude=True)
    gateway_plugin: str = Field(default="", exclude=True)
    manager_cloud_available: bool = Field(default=False, exclude=True)

    def _raw_manifest_tools(self) -> List[mcp.types.Tool]:
        tools: List[mcp.types.Tool] = []
        from core.capabilities.paths import capabilities_enabled
        from core.services.management_contract import localize, declared, tools as manager_tools
        items = list(self.manifest_tools)
        if capabilities_enabled() and self.gateway_plugin == "skill-manager":
            if any(t.get("name") == "install_skill" and declared(t, self.gateway_plugin) for t in items):
                items += [t for t in manager_tools("skill-manager", "local") if t["name"] == "upload_skill_to_cloud" and not any(x.get("name") == t["name"] for x in items)]
        for item in items:
            if capabilities_enabled():
                item = localize(item, self.gateway_plugin, cloud_available=self.manager_cloud_available or bool(self.gateway_invoke_url))
                if item is None:
                    continue
            try:
                tool = mcp.types.Tool.model_validate(item)
            except Exception as exc:  # noqa: BLE001 - isolate one malformed tool
                logger.warning("Invalid manifest tool in MCP '%s': %s", self.name, exc)
                continue
            if self.enable_tools is not None and tool.name not in self.enable_tools:
                continue
            if self.disable_tools is not None and tool.name in self.disable_tools:
                continue
            if not has_usable_schema(item):
                # A manifest entry that never captured the server's inputSchema
                # would become a zero-parameter tool: the model then has nowhere
                # to put its arguments and retries the same empty call forever.
                # Withhold it instead of offering a tool that can never succeed.
                logger.warning(
                    "Manifest tool '%s' in MCP '%s' has no captured inputSchema; withheld",
                    tool.name,
                    self.name,
                )
                continue
            tools.append(tool)
        return tools

    async def list_raw_tools(self) -> List[mcp.types.Tool]:
        return self._raw_manifest_tools()

    async def get_tool(self, name: str) -> GatewayMCPTool:
        for tool in self._raw_manifest_tools():
            if tool.name == name:
                from core.capabilities.paths import capabilities_enabled
                from core.services.management_contract import declared
                if capabilities_enabled() and declared(tool.model_dump(by_alias=True), self.gateway_plugin):
                    from core.llm.management_tool import ManagementTool
                    return ManagementTool(self.name, tool, self.gateway_plugin, self.mcp_config.headers or {}, cloud_backed=bool(self.gateway_invoke_url))
                return GatewayMCPTool(
                    mcp_name=self.name,
                    tool=tool,
                    invoke_url=self.gateway_invoke_url,
                    schema_hash=self.schema_hash,
                    headers=dict(self.mcp_config.headers or {}),
                    timeout=float(self.execution_timeout or 120.0),
                    transport=self.gateway_transport,
                    source_plugin=self.gateway_plugin,
                )
        raise ValueError(f"Tool '{name}' not found in cloud manifest MCP '{self.name}'")

    async def list_tools(self) -> List[GatewayMCPTool]:
        return [await self.get_tool(tool.name) for tool in self._raw_manifest_tools()]


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


# The factory owns transient clients until it returns them to its caller.
# ContextVar values propagate into parallel HTTP probes, so partial construction
# has one cleanup owner even when cancellation interrupts asyncio.gather.
_factory_clients: ContextVar[list | None] = ContextVar("factory_mcp_clients", default=None)


def own_factory_client(client):
    clients = _factory_clients.get()
    if clients is not None:
        clients.append(client)
    return client


def close_factory_clients_on_error(function):
    @wraps(function)
    async def construct(*args, **kwargs):
        clients = []
        token = _factory_clients.set(clients)
        try:
            return await function(*args, **kwargs)
        except BaseException:
            await close_clients(list(reversed(clients)))
            raise
        finally:
            _factory_clients.reset(token)

    return construct


async def connect_mcp_clients(
    mcp_servers: Dict[str, dict],
) -> List[MCPClient]:
    """Connect to MCP servers and return the connected MCPClient list."""
    clients: List[MCPClient] = []
    for server_name, server_cfg in mcp_servers.items():
        client = make_stdio_client(server_name, server_cfg)
        try:
            await client.connect()
            clients.append(own_factory_client(client))
            logger.debug("MCP client '%s' connected", server_name)
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception as exc:
            logger.warning("Failed to connect MCP server '%s': %s", server_name, exc)
            try:
                await client.close()
            except Exception:
                pass
    return clients


async def close_clients(clients: List[MCPClient]) -> None:
    """Close every client in its owning task, deferring repeated cancellation."""
    cancelled = None
    task = asyncio.current_task()
    for client in clients:
        while True:
            before = task.cancelling() if task else 0
            try:
                await client.close()
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
                if task is not None and task.cancelling() > before:
                    # An external repeat-cancel interrupted this close. Finish
                    # this same client's cleanup on the same task, then the rest.
                    continue
                # A client scope itself signalled cancellation. It must not
                # prevent independent clients from releasing their resources.
                logger.debug("MCP client close signalled cancellation")
                break
            except Exception as exc:
                logger.debug("Error closing MCP client: %s", exc)
                break
    if cancelled is not None:
        raise cancelled
