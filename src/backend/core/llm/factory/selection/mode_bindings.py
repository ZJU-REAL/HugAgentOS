"""Agent assembly phase: mode bindings. """

from __future__ import annotations

from typing import List

from core.llm.factory.models import RequiredCapabilities
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def mode_bindings(request: AgentRequest, *, required: RequiredCapabilities):
    _turbo_code_exec = False
    _mode_plugin_ids: List[str] = []
    _mode_manual_invoke = True
    if request.turbo_mode:
        if request.mode_spec is not None:
            # 模式表是真源：这几个取值全部来自 chat_modes 那一行。
            _mode_manual_invoke = bool(getattr(request.mode_spec, "manual_invoke_enabled", True))
            _mode_mcp_ids = list(getattr(request.mode_spec, "mcp_server_ids", ()) or ())
            _mode_skill_ids = list(getattr(request.mode_spec, "skill_ids", ()) or ())
            _mode_plugin_ids = list(getattr(request.mode_spec, "plugin_ids", ()) or ())
            _turbo_code_exec = bool(getattr(request.mode_spec, "code_exec_enabled", False))
        else:
            from core.services.system_config import (
                turbo_manual_invoke_enabled,
                turbo_mcp_server_ids,
                turbo_plugin_ids,
                turbo_skill_ids,
            )

            _mode_manual_invoke = turbo_manual_invoke_enabled()
            _mode_mcp_ids = sorted(turbo_mcp_server_ids())
            _mode_skill_ids = list(turbo_skill_ids())
            _mode_plugin_ids = list(turbo_plugin_ids())

        if not _mode_manual_invoke:
            # Manual summoning disabled by ops: turbo is strictly its own set.
            if required.connector_ids or required.plugin_id or required.skill_id:
                raise RuntimeError(
                    "当前对话模式禁止手动调用技能、连接器或插件；"
                    "本轮已停止，未静默忽略用户选择。"
                )
            request.turbo_explicit_skill_ids = None
            request.turbo_explicit_mcp_ids = None
            request.visible_subagents = None
        # Admin-configured turbo plugins, expanded into their component skills +
        # MCPs. A plugin is the installable/removable unit — some capabilities
        # (e.g. a crawler, a ticket system) ship only as a plugin and have no
        # loose MCP row to pick, so without this they were unreachable in turbo.
        turbo_plugin_skill_ids, turbo_plugin_mcp_ids = factory_capabilities._expand_plugin_bindings(
            list(_mode_plugin_ids), user_id=request.current_user_id
        )
        # Skills in turbo = admin-configured set (turbo.skill_ids + those bundled
        # with a configured plugin) + the ones explicitly summoned this turn.
        # With none of the three the agent carries no skills at all (and no skill
        # list enters the prompt) — the original quick-lookup contract.
        request.enabled_skill_ids = list(
            dict.fromkeys(
                [
                    *_mode_skill_ids,
                    *[s for s in turbo_plugin_skill_ids if isinstance(s, str) and s.strip()],
                    *[
                        s
                        for s in (request.turbo_explicit_skill_ids or [])
                        if isinstance(s, str) and s.strip()
                    ],
                ]
            )
        )
        request.top_level_chat = False
        if not _turbo_code_exec:
            request.read_only = True
            request.allow_bash = False
        # The turbo tool surface comes from admin config alone — deliberately
        # NOT intersected with catalog/user enable-disable state. Explicitly
        # summoned plugin MCPs are appended on top; MCPs bound to a summoned
        # skill merge in the shared binding step below.
        request.enabled_mcp_ids = list(
            dict.fromkeys(
                [
                    *_mode_mcp_ids,
                    *[m for m in turbo_plugin_mcp_ids if isinstance(m, str) and m.strip()],
                    *[
                        m
                        for m in (request.turbo_explicit_mcp_ids or [])
                        if isinstance(m, str) and m.strip()
                    ],
                ]
            )
        )

    return (
        _mode_manual_invoke,
        _mode_plugin_ids,
        _turbo_code_exec,
    )
