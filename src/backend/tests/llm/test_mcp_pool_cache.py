"""Tests for MCPConnectionPool (AgentScope 2.0 rewrite).

Migration note: the 1.x "tool-function cache" (_cached_tool_funcs / get_callable_function /
build_toolkit_from_cache returning a Toolkit) no longer exists in 2.0 -- MCPClient has no
get_callable_function, and the Toolkit is constructed once. This pool is now responsible only for **connection reuse**:
  • only pools stdio stable servers (HTTP is not pooled even when is_stable, it goes per-request stateless)
  • get_request_clients() returns a list of connected MCPClients (including stable + transient)
  • shutdown / reconnect / reinitialize lifecycle

This file is rewritten accordingly, testing 2.0's actual pool behavior.
"""

from __future__ import annotations

from typing import Dict, List, Optional
from unittest.mock import patch

import pytest


# ── Fake MCPClient ─────────────────────────────────────────────────────────

class FakeTool:
    def __init__(self, name: str):
        self.name = name
        self.description = ""


class FakeMCPClient:
    """Mimics agentscope 2.0 MCPClient (connect/list_tools/close)."""

    def __init__(self, name: str, tools: Optional[List[str]] = None):
        self.name = name
        self._tools = [FakeTool(t) for t in (tools or ["tool_a", "tool_b"])]
        self.connect_count = 0
        self.close_count = 0
        self.list_tools_count = 0

    async def connect(self):
        self.connect_count += 1

    async def list_tools(self):
        self.list_tools_count += 1
        return list(self._tools)

    async def close(self):
        self.close_count += 1


def _make_pool():
    from core.llm.mcp_pool import MCPConnectionPool
    pool = MCPConnectionPool.__new__(MCPConnectionPool)
    pool.__init__()
    return pool


def _stdio_configs(names: List[str], stable: bool = True) -> Dict[str, dict]:
    return {
        name: {"command": "python", "args": ["-m", f"mcp_servers.{name}"], "is_stable": stable}
        for name in names
    }


def _http_config(name: str, stable: bool = True) -> Dict[str, dict]:
    return {name: {"transport": "streamable_http", "url": f"http://{name}/mcp", "is_stable": stable}}


@pytest.fixture
def pool():
    return _make_pool()


def _patch_make_client(registry: dict):
    def fake_make_client(name, cfg, *, is_stateful=True):
        c = FakeMCPClient(name=name)
        registry[name] = c
        return c
    return patch("core.llm.mcp_pool.make_client", side_effect=fake_make_client)


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_connects_stdio_stable(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["server_a", "server_b"]))
        assert pool.is_initialized
        assert pool.stable_client_count == 2
        assert pool.has_cached_tools  # 2.0 semantics: stable connections already exist
        assert clients["server_a"].connect_count == 1
        assert clients["server_b"].connect_count == 1

    @pytest.mark.asyncio
    async def test_http_not_pooled(self, pool):
        """An HTTP server does not enter the stable pool even when is_stable (2.0: goes per-request stateless)."""
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_http_config("kb_http"))
        assert pool.stable_client_count == 0
        assert "kb_http" not in pool.stable_server_ids

    @pytest.mark.asyncio
    async def test_connect_failure_does_not_block_init(self, pool):
        def fake_make_client(name, cfg, *, is_stateful=True):
            c = FakeMCPClient(name=name)
            if name == "bad":
                async def fail():
                    raise RuntimeError("connect failed")
                c.connect = fail
            return c
        with patch("core.llm.mcp_pool.make_client", side_effect=fake_make_client):
            await pool.initialize(_stdio_configs(["good", "bad"]))
        assert pool.is_initialized
        assert "good" in pool._stable_clients
        assert "bad" not in pool._stable_clients


class TestGetRequestClients:
    @pytest.mark.asyncio
    async def test_returns_stable_clients(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["srv_a", "srv_b"]))
            req, transient = await pool.get_request_clients(enabled_keys=["srv_a", "srv_b"])
        assert {c.name for c in req} == {"srv_a", "srv_b"}
        assert transient == []

    @pytest.mark.asyncio
    async def test_partial_enabled_keys(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["srv_a", "srv_b", "srv_c"]))
            req, _ = await pool.get_request_clients(enabled_keys=["srv_a"])
        assert {c.name for c in req} == {"srv_a"}

    @pytest.mark.asyncio
    async def test_transient_spawned_fresh(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["stable_srv"]))
            req, transient = await pool.get_request_clients(
                enabled_keys=["stable_srv", "transient_srv"],
                per_request_servers_cfg=_stdio_configs(["transient_srv"], stable=False),
            )
        assert len(transient) == 1
        assert transient[0].name == "transient_srv"
        assert transient[0].connect_count == 1

    @pytest.mark.asyncio
    async def test_build_toolkit_from_cache_alias(self, pool):
        """The backward-compatible alias build_toolkit_from_cache now returns (clients, transient)."""
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["srv"]))
            req, transient = await pool.build_toolkit_from_cache(enabled_keys=["srv"])
        assert {c.name for c in req} == {"srv"}


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_shutdown_closes_and_clears(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["srv"]))
        assert pool.has_cached_tools
        await pool.shutdown()
        assert not pool.has_cached_tools
        assert pool.stable_client_count == 0
        assert clients["srv"].close_count == 1

    @pytest.mark.asyncio
    async def test_reconnect_stable(self, pool):
        clients: dict = {}
        with _patch_make_client(clients):
            await pool.initialize(_stdio_configs(["srv"]))
            new_client = await pool._reconnect_stable("srv")
        assert new_client is not None
        assert new_client.connect_count == 1

    @pytest.mark.asyncio
    async def test_reinitialize_on_config_change(self, pool):
        with _patch_make_client({}):
            await pool.initialize(_stdio_configs(["srv_a"]))
            assert pool.stable_server_ids == frozenset({"srv_a"})
            await pool.reinitialize_if_config_changed(_stdio_configs(["srv_a", "srv_b"]))
            assert pool.stable_server_ids == frozenset({"srv_a", "srv_b"})


class TestHasCachedTools:
    @pytest.mark.asyncio
    async def test_false_before_init(self, pool):
        assert not pool.has_cached_tools

    @pytest.mark.asyncio
    async def test_true_after_init_with_stable(self, pool):
        with _patch_make_client({}):
            await pool.initialize(_stdio_configs(["srv"]))
        assert pool.has_cached_tools

    @pytest.mark.asyncio
    async def test_false_when_no_stable_servers(self, pool):
        await pool.initialize(_stdio_configs([], stable=True))
        assert not pool.has_cached_tools
