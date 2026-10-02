"""Agent assembly phase: tool surface. """

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from agentscope.tool import Toolkit
from core.capabilities import runtime as capability_runtime
from core.llm.factory.models import RequiredCapabilities
from core.llm.factory.request import AgentRequest
from core.ontology.toolkit import OntologyFilteredToolkit
from core.ontology.validator import register_runtime_asset_tags


async def tool_surface(
    request: AgentRequest,
    *,
    _SKILL_INSTRUCTION_TEMPLATE,
    _elapsed,
    _log,
    _note_unavailable,
    _prepared_capabilities,
    required: RequiredCapabilities,
    _required_connector_tool_names,
    _required_plugin_mcp_tool_names,
    http_clients,
    loader,
    mcp_clients,
    skill_ids_to_register,
    toolkit,
):
    _log.info("[factory] +%s tools registered", _elapsed())
    _agent_ref: Optional[Dict] = None
    # Progressive plugin runtime holder — filled in stages: registered with the
    # collector below, then completed after the real Toolkit / AgentRuntimeState
    # exist (the load_plugin closure mutates both at activation time).
    _plugin_runtime: Optional[Dict[str, Any]] = None

    _ontology_runtime = (
        request.ontology_runtime if isinstance(request.ontology_runtime, dict) else {}
    )
    skill_metadata = loader.load_all_metadata()
    _log.info(
        "[factory] +%s skill metadata loaded (%d)",
        _elapsed(),
        len(skill_metadata or {}),
    )
    for skill_id in skill_ids_to_register or []:
        metadata = skill_metadata.get(skill_id)
        register_runtime_asset_tags(
            _ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            tags=list(getattr(metadata, "tags", []) or []),
        )
    for visible_agent in request.visible_subagents or []:
        extra = visible_agent.get("extra_config") or {}
        tags = list(visible_agent.get("ontology_tags") or [])
        tags.extend(extra.get("ontology_tags") or [])
        register_runtime_asset_tags(
            _ontology_runtime,
            kind="subagent",
            asset_id=str(visible_agent.get("agent_id") or ""),
            tags=tags,
        )
    _ontology_hidden_tools = {
        tool_name
        for pack in _ontology_runtime.get("packs", [])
        for workflow in pack.get("workflows", [])
        for tool_name in workflow.get("forbidden_tools", [])
    }

    def _build_toolkit(*, include_skills: bool = True) -> Toolkit:
        # ``toolkit`` here is still the ToolCollector; construct the real Toolkit from the current collection state.
        #
        # "Skill" (AgentScope's builtin SkillViewer) is hidden unconditionally —
        # distinct from the ontology forbidden_tools above. The Toolkit injects
        # its schema on every request whenever any skill is registered, but this
        # stack loads skills exclusively through view_text_file (sandbox↔backend
        # path mapping, {baseDir} substitution, runtime hint, skill_call
        # observability). A model that called Skill instead would bypass all of
        # that and receive backend-path content unusable in the sandbox — so the
        # schema is pure per-round prefill waste plus a wrong door.
        _visible_mcps = [*mcp_clients, *http_clients]
        if _prepared_capabilities is not None:
            from core.llm.capability_tools import AvailableMCPClient

            _live_run = (
                capability_runtime.get(
                    _prepared_capabilities.run_id, scope_id=request.capability_scope
                )
                or _prepared_capabilities
            )
            _visible_mcps = [AvailableMCPClient(client, _live_run) for client in _visible_mcps]
        return OntologyFilteredToolkit(
            tools=toolkit.function_tools,
            mcps=_visible_mcps,
            skills_or_loaders=(toolkit.skill_loaders or None) if include_skills else None,
            skill_instruction_template=_SKILL_INSTRUCTION_TEMPLATE,
            hidden_tools={*_ontology_hidden_tools, "Skill"},
        )

    # Compute schemas first in the "subagent tools not yet registered" state
    # (consistent with 1.x: subagent tools are registered after
    # get_json_schemas, so they don't enter the system_prompt's tool list).
    # Skill's built-in viewer is hidden unconditionally. This preliminary
    # tool-only surface does not need to enumerate every skill; the final
    # toolkit still loads/validates the complete visible skill instructions.
    tool_schemas = await _build_toolkit(include_skills=False).get_tool_schemas()
    visible_tool_names = {
        str(schema.get("function", {}).get("name") or "")
        for schema in tool_schemas
        if isinstance(schema, dict)
    }
    if _required_connector_tool_names:
        _required_connector_tool_names = [
            name for name in _required_connector_tool_names if name in visible_tool_names
        ]
        if not _required_connector_tool_names:
            _note_unavailable("所选连接器当前没有可调用工具。")
    _required_skill_registered = bool(
        required.skill_id
        and required.skill_id in (skill_ids_to_register or [])
        and loader.get_skill_dir(required.skill_id)
        and "view_text_file" in visible_tool_names
    )
    if required.skill_id and not _required_skill_registered:
        _note_unavailable(f"所选技能「{required.skill_name}」的说明文件暂不可用。")
    _required_plugin_mcp_tool_names = [
        name for name in _required_plugin_mcp_tool_names if name in visible_tool_names
    ]
    _required_plugin_registered_skill_ids = [
        skill_id
        for skill_id in required.plugin_skill_ids
        if skill_id in (skill_ids_to_register or [])
        and loader.get_skill_dir(skill_id)
        and "view_text_file" in visible_tool_names
    ]
    if required.plugin_id and not (
        _required_plugin_registered_skill_ids or _required_plugin_mcp_tool_names
    ):
        _note_unavailable(f"所选插件「{required.plugin_name}」当前没有可执行能力。")
    if (
        required.plugin_id
        and request.chat_id
        and (_required_plugin_registered_skill_ids or _required_plugin_mcp_tool_names)
    ):
        # Progressive loading normally persists this during its resolution.
        # Persist here as well so explicit activation is sticky in restricted
        # modes, dedicated-agent chats and when progressive loading is disabled.
        from core.plugins import runtime as _plugin_activation

        await asyncio.to_thread(
            _plugin_activation.record_plugin_activation,
            request.chat_id,
            [required.plugin_id],
            user_id=str(request.current_user_id or ""),
        )
    if request.chat_id and (_required_skill_registered or _required_connector_tool_names):
        from core.llm.session_capabilities import record_session_capability_activation

        await asyncio.to_thread(
            record_session_capability_activation,
            request.chat_id,
            skill_ids=[required.skill_id] if _required_skill_registered else None,
            mcp_ids=required.connector_ids if _required_connector_tool_names else None,
        )
    _log.info("[factory] +%s tool schemas computed (%d)", _elapsed(), len(tool_schemas or []))
    # update_plan 只在下面的非子智能体分支里注册；先给默认值，子智能体分支走完
    # 也能安全判断「要不要挂计划催更中间件」。
    _plan_tool_enabled = False

    return (
        _agent_ref,
        _build_toolkit,
        _ontology_hidden_tools,
        _ontology_runtime,
        _plan_tool_enabled,
        _plugin_runtime,
        tool_schemas,
    )
