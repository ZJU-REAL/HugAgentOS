"""Ordered assembly; each phase has explicit inputs and outputs."""

from __future__ import annotations

from core.llm.factory.prompts.context import prompt_context
from core.llm.factory.prompts.system import system_prompt as assemble_system_prompt
from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime.agent_construction import agent_construction
from core.llm.factory.runtime.execution_budget import execution_budget
from core.llm.factory.runtime.listeners import runtime_listeners
from core.llm.factory.runtime.manifest_binding import manifest_binding
from core.llm.factory.runtime.middleware_stack import middleware_stack
from core.llm.factory.runtime.model_selection import model_selection
from core.llm.factory.selection.device_bindings import device_bindings
from core.llm.factory.selection.device_preparation import device_preparation
from core.llm.factory.selection.entry_policy import entry_policy
from core.llm.factory.selection.mode_bindings import mode_bindings
from core.llm.factory.selection.profile_selection import profile_selection
from core.llm.factory.selection.progressive_bindings import progressive_bindings
from core.llm.factory.selection.server_selection import server_selection
from core.llm.factory.selection.sticky_capabilities import sticky_capabilities
from core.llm.factory.selection.subagent_bindings import subagent_bindings
from core.llm.factory.tools.interactive import interactive_tools
from core.llm.factory.tools.mcp_connections import mcp_connections
from core.llm.factory.tools.native import native_tools
from core.llm.factory.tools.skill_registration import skill_registration
from core.llm.factory.tools.tool_surface import tool_surface


async def assemble_agent(request: AgentRequest):
    _caps_skill_ids = None
    (
        _api_scope,
        _capability_notices,
        _capability_run_key,
        _elapsed,
        _eval_scope,
        _log,
        _note_unavailable,
        required,
        _shared_subagent_cfg,
        sticky,
    ) = await entry_policy(request)
    (
        _subagent_progressive,
        cfg,
    ) = await subagent_bindings(request, _api_scope=_api_scope, _elapsed=_elapsed, _log=_log)
    (
        _mode_manual_invoke,
        _mode_plugin_ids,
        _turbo_code_exec,
    ) = await mode_bindings(
        request,
        required=required,
    )
    (sticky,) = await sticky_capabilities(
        request,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _mode_manual_invoke=_mode_manual_invoke,
        _note_unavailable=_note_unavailable,
        required=required,
        sticky=sticky,
    )
    (
        _progressive,
        skill_bound_mcp_ids,
        skill_ids_for_bindings,
    ) = await progressive_bindings(request, _eval_scope=_eval_scope, _log=_log)
    (
        _manifest_builder,
        profile,
        skill_ids_for_bindings,
        skill_selection,
    ) = await profile_selection(
        request,
        _log=_log,
        required=required,
        sticky=sticky,
        skill_ids_for_bindings=skill_ids_for_bindings,
    )
    (
        _capability_mcp_resolution,
        asset_bundle,
        bridge_mcp_servers,
        enabled_mcp_keys,
        owned_mcp_servers,
    ) = await server_selection(
        request,
        _api_scope=_api_scope,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _note_unavailable=_note_unavailable,
        required=required,
        sticky=sticky,
        cfg=cfg,
        skill_bound_mcp_ids=skill_bound_mcp_ids,
    )
    (
        _caps_skill_ids,
        _desktop_progressive,
        _prepared_capabilities,
    ) = await device_preparation(
        request,
        _api_scope=_api_scope,
        _capability_run_key=_capability_run_key,
        _caps_skill_ids=_caps_skill_ids,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _mode_plugin_ids=_mode_plugin_ids,
        _note_unavailable=_note_unavailable,
        required=required,
        sticky=sticky,
        enabled_mcp_keys=enabled_mcp_keys,
    )
    (
        _desktop_prepared_servers,
        _prepared_capabilities,
        _progressive,
        _required_connector_server_keys,
        _required_plugin_server_keys,
        _subagent_progressive,
        enabled_mcp_keys,
        enabled_servers,
    ) = await device_bindings(
        request,
        _api_scope=_api_scope,
        _capability_mcp_resolution=_capability_mcp_resolution,
        _caps_skill_ids=_caps_skill_ids,
        _desktop_progressive=_desktop_progressive,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _mode_plugin_ids=_mode_plugin_ids,
        _note_unavailable=_note_unavailable,
        _prepared_capabilities=_prepared_capabilities,
        _progressive=_progressive,
        required=required,
        sticky=sticky,
        _subagent_progressive=_subagent_progressive,
        bridge_mcp_servers=bridge_mcp_servers,
        enabled_mcp_keys=enabled_mcp_keys,
        owned_mcp_servers=owned_mcp_servers,
    )
    (
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
    ) = await mcp_connections(
        request,
        _api_scope=_api_scope,
        _elapsed=_elapsed,
        _log=_log,
        _note_unavailable=_note_unavailable,
        required=required,
        _required_connector_server_keys=_required_connector_server_keys,
        _required_plugin_server_keys=_required_plugin_server_keys,
        enabled_mcp_keys=enabled_mcp_keys,
        enabled_servers=enabled_servers,
    )
    (
        allowed_skill_dirs,
        loader,
        skill_ids_to_register,
    ) = await skill_registration(
        request,
        _api_scope=_api_scope,
        _eval_scope=_eval_scope,
        _log=_log,
        _prepared_capabilities=_prepared_capabilities,
        loader=loader,
        toolkit=toolkit,
    )
    (
        _interactive,
        _proj_scope,
        _sbx_sess,
        loaded_skill_ids,
    ) = await interactive_tools(
        request,
        _api_scope=_api_scope,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _turbo_code_exec=_turbo_code_exec,
        allowed_skill_dirs=allowed_skill_dirs,
        loader=loader,
        skill_ids_to_register=skill_ids_to_register,
        toolkit=toolkit,
    )
    await native_tools(
        request,
        _api_scope=_api_scope,
        _eval_scope=_eval_scope,
        _interactive=_interactive,
        _is_channel_run=_is_channel_run,
        _log=_log,
        _proj_scope=_proj_scope,
        _sbx_sess=_sbx_sess,
        _shared_subagent_cfg=_shared_subagent_cfg,
        _turbo_code_exec=_turbo_code_exec,
        allowed_skill_dirs=allowed_skill_dirs,
        enabled_mcp_keys=enabled_mcp_keys,
        loaded_skill_ids=loaded_skill_ids,
        loader=loader,
        skill_ids_to_register=skill_ids_to_register,
        toolkit=toolkit,
    )
    (
        _agent_ref,
        _build_toolkit,
        _ontology_hidden_tools,
        _ontology_runtime,
        _plan_tool_enabled,
        _plugin_runtime,
        tool_schemas,
    ) = await tool_surface(
        request,
        _SKILL_INSTRUCTION_TEMPLATE=_SKILL_INSTRUCTION_TEMPLATE,
        _elapsed=_elapsed,
        _log=_log,
        _note_unavailable=_note_unavailable,
        _prepared_capabilities=_prepared_capabilities,
        required=required,
        _required_connector_tool_names=_required_connector_tool_names,
        _required_plugin_mcp_tool_names=_required_plugin_mcp_tool_names,
        http_clients=http_clients,
        loader=loader,
        mcp_clients=mcp_clients,
        skill_ids_to_register=skill_ids_to_register,
        toolkit=toolkit,
    )
    (
        _agent_ref,
        _plan_tool_enabled,
        _plugin_runtime,
        system_prompt,
    ) = await assemble_system_prompt(
        request,
        _agent_ref=_agent_ref,
        _elapsed=_elapsed,
        _log=_log,
        _manifest_builder=_manifest_builder,
        _ontology_hidden_tools=_ontology_hidden_tools,
        _ontology_runtime=_ontology_runtime,
        _plan_tool_enabled=_plan_tool_enabled,
        _plugin_runtime=_plugin_runtime,
        _progressive=_progressive,
        _sbx_sess=_sbx_sess,
        _subagent_progressive=_subagent_progressive,
        _tool_approval_available=_tool_approval_available,
        _turbo_code_exec=_turbo_code_exec,
        cfg=cfg,
        enabled_mcp_keys=enabled_mcp_keys,
        loader=loader,
        skill_ids_to_register=skill_ids_to_register,
        tool_schemas=tool_schemas,
        toolkit=toolkit,
    )
    (system_prompt,) = await prompt_context(
        request,
        _api_scope=_api_scope,
        _elapsed=_elapsed,
        _eval_scope=_eval_scope,
        _log=_log,
        _manifest_builder=_manifest_builder,
        _sbx_sess=_sbx_sess,
        profile=profile,
        system_prompt=system_prompt,
    )
    (
        _subagent_model_pinned,
        default_model,
    ) = await model_selection(
        request, _elapsed=_elapsed, _log=_log, _shared_subagent_cfg=_shared_subagent_cfg, cfg=cfg
    )
    (
        _agent_name,
        _max_iters,
        _trigger_ratio,
        context_config,
        system_prompt,
    ) = await execution_budget(
        request,
        _capability_notices=_capability_notices,
        _elapsed=_elapsed,
        _log=_log,
        _manifest_builder=_manifest_builder,
        _turbo_code_exec=_turbo_code_exec,
        default_model=default_model,
        profile=profile,
        system_prompt=system_prompt,
    )
    (
        _builtin_tool_names,
        _middlewares,
        _state,
        toolkit,
    ) = await middleware_stack(
        request,
        _api_scope=_api_scope,
        _build_toolkit=_build_toolkit,
        _eval_scope=_eval_scope,
        _interactive=_interactive,
        _ontology_runtime=_ontology_runtime,
        _plan_tool_enabled=_plan_tool_enabled,
        _proj_scope=_proj_scope,
        _sbx_sess=_sbx_sess,
        _subagent_model_pinned=_subagent_model_pinned,
        _tool_approval_available=_tool_approval_available,
        default_model=default_model,
        profile=profile,
        tool_schemas=tool_schemas,
        toolkit=toolkit,
    )
    (
        _bind_manifest,
        _compaction_system_prompt,
        _compaction_tool_schemas,
        _manifest_for_surface,
        execution_manifest,
    ) = await manifest_binding(
        request,
        _builtin_tool_names=_builtin_tool_names,
        _log=_log,
        _manifest_builder=_manifest_builder,
        profile=profile,
        skill_ids_for_bindings=skill_ids_for_bindings,
        system_prompt=system_prompt,
        tool_schemas=tool_schemas,
        toolkit=toolkit,
    )
    (agent,) = await agent_construction(
        request,
        _agent_name=_agent_name,
        _agent_ref=_agent_ref,
        _api_scope=_api_scope,
        _bind_manifest=_bind_manifest,
        _compaction_system_prompt=_compaction_system_prompt,
        _compaction_tool_schemas=_compaction_tool_schemas,
        _desktop_prepared_servers=_desktop_prepared_servers,
        _desktop_progressive=_desktop_progressive,
        _log=_log,
        _max_iters=_max_iters,
        _middlewares=_middlewares,
        _plugin_runtime=_plugin_runtime,
        _prepared_capabilities=_prepared_capabilities,
        _sbx_sess=_sbx_sess,
        _state=_state,
        _trigger_ratio=_trigger_ratio,
        asset_bundle=asset_bundle,
        context_config=context_config,
        default_model=default_model,
        execution_manifest=execution_manifest,
        system_prompt=system_prompt,
        toolkit=toolkit,
    )
    return await runtime_listeners(
        request,
        _bind_manifest=_bind_manifest,
        _elapsed=_elapsed,
        _log=_log,
        _manifest_for_surface=_manifest_for_surface,
        _plugin_runtime=_plugin_runtime,
        agent=agent,
        http_clients=http_clients,
        profile=profile,
        skill_selection=skill_selection,
        system_prompt=system_prompt,
        toolkit=toolkit,
        transient_mcp_clients=transient_mcp_clients,
    )
