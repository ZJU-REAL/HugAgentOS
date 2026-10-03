"""Agent assembly phase: manifest binding. """

from __future__ import annotations

from core.llm.execution_manifest import tool_manifest_from_schemas
from core.llm.factory.request import AgentRequest


async def manifest_binding(
    request: AgentRequest,
    *,
    _builtin_tool_names,
    _log,
    _manifest_builder,
    profile,
    skill_ids_for_bindings,
    system_prompt,
    tool_schemas,
    toolkit,
):
    def _manifest_for_surface(surface):  # noqa: ANN001, ANN202
        """Build one manifest generation from a toolkit-owned snapshot."""
        generation_builder = _manifest_builder.fork()
        tool_manifest_from_schemas(
            generation_builder,
            surface.tool_schemas,
            builtin_tool_names=_builtin_tool_names,
        )
        _effective_system_prompt = system_prompt
        _skill_instructions = surface.skill_instructions
        if _skill_instructions:
            generation_builder.add_prompt_section(
                "runtime/agent_skills",
                _skill_instructions,
                origin="agentscope:skill_registry",
                trust="configured_service",
                priority=1000,
                cache_class="capability_set",
                version="1",
                sensitive=True,
            )
            _effective_system_prompt = system_prompt + "\n" + _skill_instructions
        return generation_builder.build(
            final_prompt=_effective_system_prompt,
            surface_generation=surface.generation,
        )

    def _bind_manifest(manifest):  # noqa: ANN001, ANN202
        from core.evolution.runtime_binding import bind_runtime_assets

        return bind_runtime_assets(
            run_id=request.run_id,
            capability_scope=request.capability_scope,
            skill_ids=skill_ids_for_bindings,
            kb_ids=request.enabled_kb_ids,
            model_name=request.model_name,
            model_provider_id=request.model_provider_id,
            chat_mode=request.chat_mode,
            memory_enabled=request.memory_enabled,
            workspace_id=str(request.workspace_id or "default"),
            orchestration_profile_id=profile.profile_id,
            workflow_policy_version=profile.version,
            execution_manifest=manifest,
            manifest_required=True,
        )

    # Build the manifest from the exact same frozen surface AgentScope will use
    # for its first model request. Late tools (call_subagent/update_plan/
    # load_plugin) are already registered at this point.
    execution_manifest = None
    _compaction_tool_schemas = tool_schemas
    _compaction_system_prompt = system_prompt
    try:
        _initial_surface = await toolkit.freeze_execution_surface()
        _compaction_tool_schemas = _initial_surface.tool_schemas
        if _initial_surface.skill_instructions:
            _compaction_system_prompt = system_prompt + "\n" + _initial_surface.skill_instructions
        execution_manifest = _manifest_for_surface(_initial_surface)
        _log.info(
            "[manifest] generation=%s aggregate=%s prompt=%s tools=%s context=%s",
            execution_manifest.surface_generation,
            execution_manifest.aggregate_hash,
            execution_manifest.prompt_hash,
            execution_manifest.tool_manifest_hash,
            execution_manifest.context_hash,
        )
    except Exception as exc:  # pragma: no cover - evidence must not fail a turn
        _log.warning("[manifest] final manifest unavailable: %s", exc)

    return (
        _bind_manifest,
        _compaction_system_prompt,
        _compaction_tool_schemas,
        _manifest_for_surface,
        execution_manifest,
    )
