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
        if self.oauth_provider is None:
            return super()._create_http_client()
        config = self.mcp_config
        request_hook = getattr(self.oauth_provider, "mcp_request_hook", None)
        event_hooks = {"request": [request_hook]} if request_hook else None
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


class GatewayMCPTool(ToolBase):
    """MCP-shaped tool built from a cloud-owned runtime manifest snapshot."""

    is_mcp = True
    is_state_injected = False
    is_external_tool = False
    is_concurrency_safe = False

    def __init__(
        self,
        *,
        mcp_name: str,
        tool: mcp.types.Tool,
        invoke_url: str,
        schema_hash: str,
        headers: Dict[str, str],
        timeout: float,
        transport: Any = None,
    ) -> None:
        self.mcp_name = mcp_name
        self.name = tool.name
        self.description = tool.description or ""
        schema = dict(tool.inputSchema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        self.input_schema = schema
        self.is_read_only = bool(
            tool.annotations and getattr(tool.annotations, "readOnlyHint", False)
        )
        self._invoke_url = invoke_url
        self._schema_hash = schema_hash
        self._headers = dict(headers)
        self._timeout = max(1.0, float(timeout or 120.0))
        self._transport = transport

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        if self.is_read_only:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="This is a read-only MCP tool. Allowing execution.",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="MCP tools must be explicitly allowed by the user.",
        )

    async def __call__(self, **kwargs: Any) -> ToolChunk:
        headers = dict(self._headers)
        headers["accept-encoding"] = "identity"
        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(
                connect=min(10.0, self._timeout),
                read=self._timeout,
                write=min(60.0, self._timeout),
                pool=min(10.0, self._timeout),
            )
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    self._invoke_url,
                    headers=headers,
                    json={
                        "tool_name": self.name,
                        "arguments": kwargs,
                        "schema_hash": self._schema_hash,
                    },
                )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("cloud gateway returned no tool result")
            chunk = ToolChunk.model_validate(data)
            chunk.metadata.setdefault("origin", "cloud")
            chunk.metadata.setdefault("mcp_server_id", self.mcp_name)
            return chunk
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"云端工具 {self.name} 调用超时，请稍后重试") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                raise RuntimeError(
                    f"云端工具 {self.name} 已更新，请刷新能力清单后重试"
                ) from exc
            raise RuntimeError(
                f"云端工具 {self.name} 暂时不可用（HTTP {exc.response.status_code}）"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"云端工具 {self.name} 返回异常，请稍后重试") from exc


class ManifestMCPClient(BareNameMCPClient):
    """Network-free MCP discovery backed only by the current cloud manifest."""

    manifest_tools: List[Dict[str, Any]] = Field(default_factory=list, exclude=True)
    gateway_invoke_url: str = Field(exclude=True)
    schema_hash: str = Field(exclude=True)
    gateway_transport: Any = Field(default=None, exclude=True)

    def _raw_manifest_tools(self) -> List[mcp.types.Tool]:
        tools: List[mcp.types.Tool] = []
        for item in self.manifest_tools:
            try:
                tool = mcp.types.Tool.model_validate(item)
            except Exception as exc:  # noqa: BLE001 - isolate one malformed tool
                logger.warning("Invalid manifest tool in MCP '%s': %s", self.name, exc)
                continue
            if self.enable_tools is not None and tool.name not in self.enable_tools:
                continue
            if self.disable_tools is not None and tool.name in self.disable_tools:
                continue
            tools.append(tool)
        return tools

    async def list_raw_tools(self) -> List[mcp.types.Tool]:
        return self._raw_manifest_tools()

    async def get_tool(self, name: str) -> GatewayMCPTool:
        for tool in self._raw_manifest_tools():
            if tool.name == name:
                return GatewayMCPTool(
                    mcp_name=self.name,
                    tool=tool,
                    invoke_url=self.gateway_invoke_url,
                    schema_hash=self.schema_hash,
                    headers=dict(self.mcp_config.headers or {}),
                    timeout=float(self.execution_timeout or 120.0),
                    transport=self.gateway_transport,
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
