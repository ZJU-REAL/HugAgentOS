"""Global MCP connection pool (AgentScope 2.0).

Keeps stable MCP servers connected across requests to eliminate the 1-7s
subprocess spawn overhead. Per-request servers (e.g. retrieve_dataset_content
with runtime KB env vars) are spawned fresh each time.

Migration notes (1.x → 2.0)
---------------------------
- ``StdIOStatefulClient`` / ``HttpStatefulClient`` → ``MCPClient(is_stateful=,
  mcp_config=StdioMCPConfig|HttpMCPConfig)``.
- The 2.0 ``Toolkit`` is constructed in one shot (``Toolkit(tools=, mcps=, ...)``);
  there is **no** ``register_mcp_client`` / ``register_tool_function`` and **no**
  ``MCPClient.get_callable_function`` — so the 1.x "cache MCPToolFunction,
  register on demand" optimization cannot be ported directly. This pool now only
  handles **connection reuse** (stable clients keep their connection across
  requests, saving the subprocess spawn), returning a list of connected
  ``MCPClient`` per request, and ``agent_factory`` builds
  ``Toolkit(tools=[...FunctionTool...], mcps=clients)`` in one go.
- HTTP per-request (KB switches headers per user): use an MCPClient with
  ``is_stateful=False``; each call opens a fresh underlying connection, no
  pooling needed (see agent_factory).

Usage:
    pool = MCPConnectionPool.get_instance()
    await pool.initialize(server_configs)            # once at startup

    # Per request (stable reuses connections, per-request spawns fresh):
    clients, transient = await pool.get_request_clients(enabled_keys, per_request_cfg)
    toolkit = Toolkit(tools=fn_tools, mcps=clients)  # in agent_factory
    ...
    await pool.close_transient(transient)

    await pool.shutdown()                            # on shutdown
"""

from __future__ import annotations

import asyncio
import logging
import os
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.tool import Toolkit
from core.llm.mcp_manager import BareNameMCPClient

logger = logging.getLogger(__name__)

HTTP_TRANSPORTS = frozenset({"streamable_http", "sse"})
_KB_SERVER_ID = "retrieve_dataset_content"


def _positive_timeout(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _execution_timeout(name: str, cfg: dict) -> float:
    """Resolve a hard tool-call deadline, with a tighter KB-specific default."""
    if cfg.get("execution_timeout") is not None:
        return _positive_timeout(cfg["execution_timeout"], 120.0)
    if name == _KB_SERVER_ID:
        return _positive_timeout(os.getenv("KB_MCP_EXECUTION_TIMEOUT_SECONDS"), 75.0)
    return _positive_timeout(os.getenv("MCP_TOOL_EXECUTION_TIMEOUT_SECONDS"), 120.0)


def _http_transport_timeout(name: str, cfg: dict, execution_timeout: float) -> float:
    if cfg.get("transport_timeout") is not None:
        return _positive_timeout(cfg["transport_timeout"], 30.0)
    if name == _KB_SERVER_ID:
        # Leave a small margin beyond the MCP server's own structured timeout
        # so callers receive its tool_result instead of a transport exception.
        return execution_timeout + 5.0
    return 30.0


def is_http_cfg(cfg: dict) -> bool:
    return cfg.get("transport") in HTTP_TRANSPORTS


def make_client(
    name: str,
    cfg: dict,
    *,
    is_stateful: bool = True,
    client_cls: type[BareNameMCPClient] = BareNameMCPClient,
) -> MCPClient:
    """Build an MCPClient (HTTP or stdio) from a config dict.

    HTTP servers may set ``is_stateful=False`` for a fresh per-request connection
    (multi-user KB). stdio must be stateful (enforced by 2.0). ``client_cls`` lets
    callers substitute a ``BareNameMCPClient`` subclass (e.g. the confirmation-gated
    client) while reusing the same URL/config construction.
    """
    execution_timeout = _execution_timeout(name, cfg)
    if is_http_cfg(cfg):
        # ⚠️ The 2.0 streamable_http MCP client does not follow 307 redirects. Many MCP
        # servers 307-redirect ``/mcp/`` (trailing slash) to ``/mcp`` → 2.0 throws
        # HTTPStatusError outright and triggers an anyio cancel-scope crash. Strip the
        # trailing slash to match the server's canonical form and avoid the redirect.
        _url = (cfg.get("url") or "").rstrip("/")
        return client_cls(
            name=name,
            is_stateful=is_stateful,
            mcp_config=HttpMCPConfig(
                url=_url,
                headers=cfg.get("headers") or None,
                timeout=_http_transport_timeout(name, cfg, execution_timeout),
            ),
            execution_timeout=execution_timeout,
        )
    return client_cls(
        name=name,
        is_stateful=True,
        mcp_config=StdioMCPConfig(
            command=cfg.get("command", "python"),
            args=cfg.get("args", []),
            env=cfg.get("env") or None,
        ),
        execution_timeout=execution_timeout,
    )


class MCPConnectionPool:
    """Singleton MCP connection pool (connection reuse only, 2.0)."""

    _instance: Optional[MCPConnectionPool] = None
    _instance_lock = Lock()

    def __init__(self) -> None:
        self._stable_clients: Dict[str, MCPClient] = {}
        self._stable_configs: Dict[str, dict] = {}
        self._stable_server_ids: Set[str] = set()
        self._initialized = False
        self._config_version: int = 0
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> MCPConnectionPool:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def initialize(self, server_configs: Optional[Dict[str, dict]] = None) -> None:
        """Connect to all stable MCP servers."""
        async with self._lock:
            if server_configs is None:
                from core.services.mcp_service import McpServerConfigService

                server_configs = McpServerConfigService.get_instance().get_all_servers()

            # ⚠️ 2.0: only stdio stable servers are pooled. HTTP MCP is **not pooled** even
            # when marked is_stable — the 2.0 stateful HTTP client is task-bound, and reusing
            # it across request tasks crashes the cancel scope; HTTP always goes per-request
            # with is_stateful=False (see agent_factory._connect_http).
            self._stable_server_ids = {
                name
                for name, cfg in server_configs.items()
                if cfg.get("is_stable", False) and not is_http_cfg(cfg)
            }
            await self._close_all_stable()

            stable_items = [
                (name, cfg)
                for name, cfg in server_configs.items()
                if name in self._stable_server_ids
            ]

            async def _connect_one(name: str, cfg: dict) -> bool:
                try:
                    client = make_client(name, cfg)
                    await client.connect()
                    self._stable_clients[name] = client
                    self._stable_configs[name] = cfg
                    logger.info(
                        "[mcp_pool] Connected stable server: %s (%s)",
                        name,
                        cfg.get("transport", "stdio"),
                    )
                    return True
                except Exception as exc:
                    logger.warning("[mcp_pool] Failed to connect stable server '%s': %s", name, exc)
                    return False

            results = await asyncio.gather(*(_connect_one(n, c) for n, c in stable_items))
            connected = sum(1 for ok in results if ok)
            self._initialized = True
            self._config_version += 1
            logger.info(
                "[mcp_pool] Initialized with %d/%d stable servers",
                connected,
                len(self._stable_server_ids),
            )

    async def reinitialize_if_config_changed(self, new_server_configs: Dict[str, dict]) -> None:
        new_stable_ids = {
            name
            for name, cfg in new_server_configs.items()
            if cfg.get("is_stable", False) and not is_http_cfg(cfg)
        }
        changed = new_stable_ids != self._stable_server_ids
        if not changed:
            for name in new_stable_ids:
                if self._stable_configs.get(name) != new_server_configs.get(name):
                    changed = True
                    break
        if changed:
            logger.info("[mcp_pool] Config change detected, reinitializing stable connections")
            await self.initialize(new_server_configs)

    @property
    def has_cached_tools(self) -> bool:
        # No tool-func cache in 2.0; property kept for caller compatibility (means stable servers are connected).
        return bool(self._stable_clients)

    async def get_request_clients(
        self,
        enabled_keys: List[str],
        per_request_servers_cfg: Optional[Dict[str, dict]] = None,
    ) -> Tuple[List[MCPClient], List[MCPClient]]:
        """Return the connected MCPClient list needed for this request + the transient list (for closing).

        - stable servers: reuse the already-connected client (no re-spawn).
        - per-request stdio servers: spawn fresh (concurrently).
        The caller (agent_factory) uses the returned clients in a single
        ``Toolkit(tools=, mcps=clients)``.
        """
        request_clients: List[MCPClient] = []
        transient_clients: List[MCPClient] = []

        # Phase 1: spawn per-request servers concurrently
        spawn_tasks: Dict[str, asyncio.Task] = {}
        for key in enabled_keys:
            if key not in self._stable_server_ids:
                cfg = (per_request_servers_cfg or {}).get(key)
                if cfg is not None:
                    spawn_tasks[key] = asyncio.create_task(self._spawn_transient(key, cfg))

        # Phase 2: collect stable clients (already connected, reconnect if needed)
        for key in enabled_keys:
            if key not in self._stable_server_ids:
                continue
            client = self._stable_clients.get(key)
            if client is None:
                client = await self._reconnect_stable(key)
            if client is not None:
                request_clients.append(client)

        # Phase 3: await per-request spawns
        for key, task in spawn_tasks.items():
            try:
                client = await task
                if client is not None:
                    request_clients.append(client)
                    transient_clients.append(client)
            except Exception as exc:
                logger.warning("[mcp_pool] Failed per-request server '%s': %s", key, exc)

        return request_clients, transient_clients

    # Backward-compat alias: old callers took (toolkit, transient); now returns (clients, transient).
    async def build_toolkit_from_cache(
        self,
        enabled_keys: List[str],
        per_request_servers_cfg: Optional[Dict[str, dict]] = None,
    ) -> Tuple[List[MCPClient], List[MCPClient]]:
        return await self.get_request_clients(enabled_keys, per_request_servers_cfg)

    async def close_transient(self, transient_clients: List[MCPClient]) -> None:
        for client in transient_clients:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("[mcp_pool] close transient failed: %s", exc)

    async def _spawn_transient(self, key: str, cfg: dict) -> Optional[MCPClient]:
        client = make_client(key, cfg)
        try:
            await client.connect()
            return client
        except Exception as exc:
            logger.warning("[mcp_pool] spawn_transient '%s' connect failed: %s", key, exc)
            try:
                await client.close()
            except Exception:
                pass
            raise

    async def _reconnect_stable(self, name: str) -> Optional[MCPClient]:
        cfg = self._stable_configs.get(name)
        if cfg is None:
            return None
        old = self._stable_clients.pop(name, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        try:
            client = make_client(name, cfg)
            await client.connect()
            self._stable_clients[name] = client
            logger.info("[mcp_pool] Reconnected stable server: %s", name)
            return client
        except Exception as exc:
            logger.warning("[mcp_pool] Reconnection failed for '%s': %s", name, exc)
            return None

    async def _close_all_stable(self) -> None:
        for name, client in list(self._stable_clients.items()):
            try:
                await client.close()
            except Exception as exc:
                logger.warning("[mcp_pool] close failed for '%s': %s", name, exc)
        self._stable_clients.clear()
        self._stable_configs.clear()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._close_all_stable()
            self._initialized = False
            logger.info("[mcp_pool] Shut down")

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def stable_client_count(self) -> int:
        return len(self._stable_clients)

    @property
    def stable_server_ids(self) -> frozenset[str]:
        return frozenset(self._stable_server_ids)
