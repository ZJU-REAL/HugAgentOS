"""Agent assembly phase: mcp connections. """

from __future__ import annotations
from core.capabilities.skills import loader_factory

import asyncio
from typing import List

from core.llm.factory.defaults import _SKILL_INSTRUCTION_TEMPLATE
from core.llm.factory.models import RequiredCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.mcp.manager import own_factory_client
from core.llm.factory.tools.definitions import ToolAssembly
from core.sandbox.permissions.authority import ApprovalAuthority


def _leaf_errors(exc):
    """Task groups wrap the transport's real error; report the leaves, not the wrapper."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaf_errors(inner)
    else:
        yield exc


# Admin-declared per-tool policy (``admin_mcp_servers.extra_config.tool_permissions``):
# server annotations are only hints, so a tool that must be confirmed is declared by
# the platform side. ``ask_destructive`` is confirmed even under the "approve for me"
# preset; only "fully open" and trusted unattended runs answer it automatically.
_DECLARED_OPS = {"ask": "execute", "ask_destructive": "delete"}


def _gate_tools(resource, specs, op_for, *, kind, summary):
    """Route the tools ``op_for`` selects through the approval gate (never trusting annotations)."""
    from core.sandbox.permissions.dispatch_models import DOMAIN_APPROVAL, PermissionIntent, ToolPermissionSpec

    for tool in resource.tools:
        op = op_for(tool)
        if op is None:
            continue
        if tool.name in specs:
            raise ValueError(f"Duplicate {kind} tool name")
        target = f"{resource.name}:{tool.name}"
        text = summary(tool)

        def resolve(_args, _runtime, target=target, op=op, text=text):
            return (PermissionIntent(domain=DOMAIN_APPROVAL, action="execute", target=target,
                                     summary=text, op=op),)

        key = f"{kind}-mcp:{target}" + ("" if kind == "client" else f":{op}")
        tool.dispatch_spec = specs[tool.name] = ToolPermissionSpec(key, resolve)


def _gate_declared_tools(resource, declared, specs):
    if isinstance(declared, dict) and declared:
        _gate_tools(resource, specs, lambda tool: _DECLARED_OPS.get(str(declared.get(tool.name) or "")),
                    kind="declared", summary=lambda tool: f"运行「{resource.display_name}」的 {tool.name}")


def _gate_client_tools(resource, specs):
    """Client-provided tools pass the ordinary approval gate (unless already declared stricter)."""
    _gate_tools(resource, specs,
                lambda tool: None if getattr(tool, "dispatch_spec", None) is not None else "execute",
                kind="client", summary=lambda tool: "Run client-provided tool")


async def mcp_connections(
    request: AgentRequest,
    *,
    _authority,
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
        loader = loader_factory.get_skill_loader()
        loader.load_all_metadata()
        return loader

    # DB prompt parts are now pre-loaded at startup via warmup_prompt_cache(),
    # so no need to fetch them per-request.
    # DB-driven env overlays are already applied inside McpServerConfigService,
    # so no manual overlay step is needed here.

    loader = await asyncio.to_thread(_preload_skill_metadata)
    _log.info("[factory] +%s skill metadata pre-loaded", _elapsed())

    mcp_resources = []
    toolkit = ToolAssembly()
    # ⚠️ This is a Jinja2 template, rendered by toolkit.get_skill_instructions()
    # with the ``skills`` variable. It MUST contain the
    # ``{% for skill in skills %}`` loop to actually list the skills — otherwise
    # only the header prints, the skill list is entirely empty, and the model
    # sees no skills and never auto-triggers them (a missing loop once made all
    # skills effectively unloaded, invocable only manually via /). Keeps the
    # Chinese view_text_file guidance + restores the skill-list loop.
    #
    # Skill.dir is supplied by the deployment's path policy.

    # Native tools and registry-governed MCP tools share the permission middleware.
    # Other MCP servers retain their own declared permission behavior.
    _is_channel_run = bool((request.channel_origin or {}).get("channel_id"))
    _tool_approval_available = bool(
        request.chat_id is not None
        and not request.isolated
        and not request.plan_mode
        and not _is_channel_run
        and not request.automation_run
        and _authority is not ApprovalAuthority.CREDENTIAL
    )

    if not request.disable_tools and enabled_servers:
        from core.llm.mcp.lifetime import discover
        from core.harness.registry.local_publication import ensure_native_servers

        async def load(key, cfg):
            cfg = {**cfg, "read_only": request.read_only}
            try:
                if cfg.get("schema_source") not in {"cloud_manifest", "plugin_manifest"}:
                    await ensure_native_servers({key: cfg})
                from core.llm.mcp.callbacks import Interactions
                interactions = Interactions(request, key, _tool_approval_available)
                scope = ((request.current_user_id, request.chat_id, request.capability_scope)
                         if request.current_user_id and request.chat_id and not request.isolated else None)
                resource = await discover(key, cfg, interactions=interactions, scope=scope)
                resource.client_session_lease = cfg.get("client_session_lease")
                resource.display_name = cfg.get("display_name") or key
                resource.tool_permissions = cfg.get("tool_permissions") or {}
                return own_factory_client(resource)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if cfg.get("client_session_lease"):
                    _log.warning("Client MCP discovery failed: %s", type(exc).__name__)
                    raise RuntimeError("Client MCP discovery failed") from None
                target = str(cfg.get("url") or cfg.get("command") or "").split("?", 1)[0]
                _log.warning("MCP %s discovery failed (%s %s): %s", key, cfg.get("transport") or "stdio", target,
                             "; ".join(f"{type(e).__name__}: {e}"[:300] for e in _leaf_errors(exc)))
                return None

        results = await asyncio.gather(*(load(k, c) for k, c in enabled_servers.items()))
        mcp_resources = [resource for resource in results if resource is not None]
        failed = [key for key, resource in zip(enabled_servers, results) if resource is None]
        if failed:
            _note_unavailable("以下 MCP 服务连接失败，本轮无法使用其工具：" + "、".join(failed))

    for resource in mcp_resources:
        # Declared policy holds for every transport; client tools are then gated too.
        _gate_declared_tools(resource, resource.tool_permissions, toolkit.client_permission_specs)
        if resource.client_session_lease:
            _gate_client_tools(resource, toolkit.client_permission_specs)

    connected_clients = {resource.name: resource for resource in mcp_resources}

    async def _mcp_tool_names_for_servers(server_keys: List[str], *, log_prefix: str) -> List[str]:
        return list(dict.fromkeys(
            tool.name for key in server_keys if key in connected_clients
            for tool in connected_clients[key].tools
        ))

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
        mcp_resources,
        loader,
        toolkit,
    )
