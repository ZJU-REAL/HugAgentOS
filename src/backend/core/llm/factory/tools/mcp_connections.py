"""Agent assembly phase: mcp connections. """

from __future__ import annotations

import asyncio
import time
from typing import List

import core.agent_skills.loader as skill_loader
from agentscope.mcp import MCPClient
from core.llm.factory.defaults import (
    _HTTP_MCP_FAIL_AT,
    _HTTP_MCP_FAIL_COOLDOWN_S,
    _SKILL_INSTRUCTION_TEMPLATE,
    KB_MCP_HTTP_URL,
)
from core.llm.factory.models import RequiredCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.mcp_manager import own_factory_client
from core.llm.mcp_pool import MCPConnectionPool
from core.llm.tool_collector import ToolCollector


async def mcp_connections(
    request: AgentRequest,
    *,
    _api_scope,
    _elapsed,
    _log,
    _note_unavailable,
    required: RequiredCapabilities,
    _required_connector_server_keys,
    _required_plugin_server_keys,
    enabled_mcp_keys,
    enabled_servers,
):
    def _preload_skill_metadata():
        """Pre-warm skill metadata cache so registration is fast."""
        loader = skill_loader.get_skill_loader()
        loader.load_all_metadata()
        return loader

    # DB prompt parts are now pre-loaded at startup via warmup_prompt_cache(),
    # so no need to fetch them per-request.
    # DB-driven env overlays are already applied inside McpServerConfigService,
    # so no manual overlay step is needed here.

    loader = await asyncio.to_thread(_preload_skill_metadata)
    _log.info("[factory] +%s skill metadata pre-loaded", _elapsed())

    # ── Phase 2: MCP toolkit (async, may spawn per-request subprocesses) ──
    mcp_clients: List[MCPClient] = []
    # Stable (pooled) clients must be reused and must never be closed at the end
    # of a request; only transient (per-request spawned) stdio clients go on the
    # close list. mcp_clients contains stable+transient (for Toolkit
    # construction); transient_mcp_clients is only for closing.
    transient_mcp_clients: List[MCPClient] = []
    http_clients: List[MCPClient] = []
    # AgentScope 2.0: the Toolkit is constructed once — there are no incremental
    # register_* calls. Use ToolCollector to duck-type-compatibly collect our
    # in-house tools/skills (the register_* functions barely change), then
    # construct the real Toolkit at the end.
    toolkit = ToolCollector()
    # ⚠️ This is a Jinja2 template, rendered by toolkit.get_skill_instructions()
    # with the ``skills`` variable. It MUST contain the
    # ``{% for skill in skills %}`` loop to actually list the skills — otherwise
    # only the header prints, the skill list is entirely empty, and the model
    # sees no skills and never auto-triggers them (a missing loop once made all
    # skills effectively unloaded, invocable only manually via /). Keeps the
    # Chinese view_text_file guidance + restores the skill-list loop.
    #
    # Skill.dir is supplied by the deployment's path policy.

    # The declarative permission middleware governs built-in tools only. A
    # resident web conversation can answer prompts; trusted unattended entry
    # points (external channels and automation) bypass built-in policy checks.
    # MCP tools are temporarily a whitelist and never enter this registry.
    _is_channel_run = bool((request.channel_origin or {}).get("channel_id"))
    _tool_approval_available = bool(
        request.chat_id is not None
        and not request.batch_mode
        and not request.isolated
        and not request.plan_mode
        and not _is_channel_run
        and not request.automation_run
        and _api_scope is None
    )

    if not request.disable_tools and enabled_servers:
        from core.llm.mcp_pool import HTTP_TRANSPORTS, make_client, uses_manifest_schema

        http_server_cfgs = {
            k: v for k, v in enabled_servers.items() if v.get("transport") in HTTP_TRANSPORTS
        }
        stdio_servers = {
            k: v for k, v in enabled_servers.items() if v.get("transport") not in HTTP_TRANSPORTS
        }

        # ``isolated`` callers run in their own event loop (subagent_tool
        # worker threads), so they MUST NOT touch the shared MCP pool — pool
        # clients are bound to the main loop's task scope and would crash
        # anyio on cross-loop teardown. Spawn fresh per-request stdio + HTTP
        # instead, and rely on close_clients() in the caller's loop.
        if request.isolated or _api_scope is not None:
            from core.llm.mcp_manager import connect_mcp_clients

            mcp_clients = await connect_mcp_clients(stdio_servers)
            transient_mcp_clients = mcp_clients  # all freshly spawned → all closable
            per_request_http = http_server_cfgs
        else:
            pool = MCPConnectionPool.get_instance()
            pool_managed = pool.stable_server_ids if pool.is_initialized else frozenset()
            per_request_http = {k: v for k, v in http_server_cfgs.items() if k not in pool_managed}
            if pool.is_initialized:
                per_request_stdio = {
                    k: v for k, v in stdio_servers.items() if k not in pool_managed
                }
                # 2.0: the pool returns a list of connected MCPClients
                # (stable+transient); the Toolkit(mcps=...) is constructed
                # uniformly below.
                mcp_clients, transient_mcp_clients = await pool.get_request_clients(
                    enabled_keys=enabled_mcp_keys,
                    per_request_servers_cfg=per_request_stdio,
                )
            else:
                from core.llm.mcp_manager import connect_mcp_clients

                mcp_clients = await connect_mcp_clients(stdio_servers)
                transient_mcp_clients = mcp_clients  # pool off → all transient

        # Per-request HTTP — pool can't carry per-request headers that some
        # servers (e.g. retrieve_dataset_content) require. BaseException is
        # caught because the mcp HTTP client's SSE task can propagate
        # CancelledError on transient failures.
        async def _connect_http(key: str, cfg: dict):
            start = time.monotonic()
            last_fail = _HTTP_MCP_FAIL_AT.get(key, 0.0)
            if last_fail and start - last_fail < _HTTP_MCP_FAIL_COOLDOWN_S:
                return None
            # ⚠️ 2.0 key point: HTTP MCP uses is_stateful=False (a new connection
            # per call), avoiding the stateful client's task-binding problem
            # (the connect task differs from the request task → cancel-scope
            # crash, so tool_result is never received). Stateless clients need
            # not and must not connect(); a single list_tools serves as the
            # liveness probe (lazy connect + enumerate, verifying reachability),
            # after which the Toolkit opens a fresh connection on every call.
            _http_cfg = {**cfg, "url": cfg.get("url", KB_MCP_HTTP_URL)}
            if _http_cfg.get("schema_source") == "cloud_manifest" and not uses_manifest_schema(
                _http_cfg
            ):
                _log.warning(
                    "[factory] HTTP MCP '%s' ignored because its cloud manifest is invalid",
                    key,
                )
                return None
            client = own_factory_client(make_client(key, _http_cfg, is_stateful=False))
            if uses_manifest_schema(_http_cfg):
                # The complete, authorized schema arrived in the dynamic cloud
                # manifest. Agent construction is network-free; only a model-
                # selected tool call reaches the JSON gateway.
                _HTTP_MCP_FAIL_AT.pop(key, None)
                _log.info(
                    "[factory] HTTP MCP '%s' loaded from cloud manifest (%d tools)",
                    key,
                    len(_http_cfg.get("manifest_tools") or []),
                )
                return client
            try:
                await client.list_tools()
                _HTTP_MCP_FAIL_AT.pop(key, None)
                _log.info(
                    "[factory] HTTP MCP '%s' (stateless) probed in %.0fms",
                    key,
                    (time.monotonic() - start) * 1000,
                )
                return client
            except BaseException as exc:
                _HTTP_MCP_FAIL_AT[key] = time.monotonic()
                _log.warning(
                    "[factory] HTTP MCP '%s' connect failed (%s, cooldown %.0fs): %s",
                    key,
                    type(exc).__name__,
                    _HTTP_MCP_FAIL_COOLDOWN_S,
                    exc,
                )
                # Only propagate CancelledError when the *outer* task is itself
                # being cancelled (real user/system cancel). anyio's SSE-client
                # cleanup raises CancelledError as a scope-exit signal even when
                # nobody cancelled us — re-raising those was killing the whole
                # chat run whenever any single HTTP MCP (e.g. a freshly-removed
                # word_mcp / ppt_mcp / excel_mcp / pdf_mcp whose admin_mcp_servers row was still
                # ``is_enabled=true``) was unreachable.
                if isinstance(exc, asyncio.CancelledError):
                    current = asyncio.current_task()
                    if current is not None and getattr(current, "cancelling", lambda: 0)() > 0:
                        raise
                return None

        if per_request_http:
            results = await asyncio.gather(
                *(_connect_http(k, v) for k, v in per_request_http.items()),
                return_exceptions=False,
            )
            http_clients.extend(c for c in results if c is not None)

        _log.info(
            "[factory] +%s MCP tools loaded (transient_stdio=%d, http=%d)",
            _elapsed(),
            len(mcp_clients),
            len(http_clients),
        )

    connected_clients = {
        str(getattr(client, "name", "") or ""): client for client in [*mcp_clients, *http_clients]
    }

    async def _mcp_tool_names_for_servers(server_keys: List[str], *, log_prefix: str) -> List[str]:
        connected_keys = [key for key in server_keys if key in connected_clients]
        if not connected_keys:
            return []
        listed_tools = await asyncio.gather(
            *(connected_clients[key].list_tools() for key in connected_keys),
            return_exceptions=True,
        )
        names: List[str] = []
        for server_key, result in zip(connected_keys, listed_tools):
            if isinstance(result, BaseException):
                _log.warning(
                    "[%s] list_tools failed for %s: %s",
                    log_prefix,
                    server_key,
                    result,
                )
                continue
            for tool in result:
                tool_name = str(getattr(tool, "name", "") or "")
                if tool_name and tool_name not in names:
                    names.append(tool_name)
        return names

    _required_connector_tool_names = await _mcp_tool_names_for_servers(
        _required_connector_server_keys,
        log_prefix="connector-required",
    )
    if _required_connector_server_keys:
        if not _required_connector_tool_names:
            _note_unavailable(
                "所选连接器连接失败或没有可调用工具：" + ", ".join(required.connector_ids)
            )

    _required_plugin_mcp_tool_names = await _mcp_tool_names_for_servers(
        _required_plugin_server_keys,
        log_prefix="plugin-required",
    )
    if required.plugin_id and not required.plugin_skill_ids and not _required_plugin_mcp_tool_names:
        _note_unavailable(f"所选插件「{required.plugin_name}」当前没有可执行能力。")

    return (
        _SKILL_INSTRUCTION_TEMPLATE,
        _is_channel_run,
        _required_connector_tool_names,
        _required_plugin_mcp_tool_names,
        _tool_approval_available,
        http_clients,
        loader,
        mcp_clients,
        toolkit,
        transient_mcp_clients,
    )
