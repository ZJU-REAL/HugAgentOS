"""Agent assembly phase: entry policy. """

from __future__ import annotations

from typing import List

from core.llm.factory.models import RequiredCapabilities, StickyCapabilities
from core.llm.factory.request import AgentRequest


async def entry_policy(request: AgentRequest):
    """Normalize execution scopes and initialize per-construction capability state."""
    required = RequiredCapabilities()
    sticky = StickyCapabilities()
    import asyncio

    from core.llm.agent_api_runtime import parse_api_scope
    from core.llm.evaluation_runtime import evaluation_user_agent, parse_evaluation_scope
    from core.llm.middlewares import CURRENT_RUN_BINDING

    _eval_scope = parse_evaluation_scope(
        owner_user_id=request.current_user_id,
        chat_id=request.chat_id,
        session_id=request.sandbox_session_id,
    )
    if _eval_scope is not None:
        if request.agent_api_scope:
            raise ValueError("Evaluation cannot inherit an agent API execution scope")
        request.memory_enabled = False
        request.project_ctx = request.channel_origin = request.mode_spec = None
        request.top_level_chat = request.workflow_mode = request.plan_mode = request.batch_mode = (
            request.turbo_mode
        ) = False
        request.enabled_mcp_ids, request.enabled_skill_ids, request.enabled_kb_ids = [], [], []
        request.invoked_skill_ids, request.invoked_mcp_ids, request.required_mcp_ids = [], [], []
        request.required_skill_id = request.required_skill_name = request.required_plugin_id = (
            request.required_plugin_name
        ) = None
        request.required_plugin_skill_ids, request.required_plugin_mcp_ids = [], []
        request.ontology_runtime = {}
        request.workspace_id = request.sandbox_session_id = _eval_scope.session_id
        request.user_agent = evaluation_user_agent(request.user_agent)
        from core.llm.builtin_subagents import get_builtin_subagent

        request.visible_subagents = [
            item
            for item in request.visible_subagents or []
            if get_builtin_subagent(item.get("agent_id")) is not None
        ]

    _api_scope = parse_api_scope(
        request.agent_api_scope,
        owner_user_id=str(request.current_user_id or ""),
        chat_id=request.chat_id,
        agent_id=getattr(request.user_agent, "agent_id", None),
    )
    if _api_scope is not None:
        request.memory_enabled = False
        request.visible_subagents = []
        request.project_ctx = None
        request.channel_origin = None
        request.top_level_chat = False
        request.workflow_mode = request.plan_mode = request.batch_mode = request.turbo_mode = False
        request.mode_spec = None
        request.sandbox_session_id = _api_scope.sandbox_session_id

    # The shared subagent role is an administrator override. Resolve it before
    # tools/vision/manifests so all surfaces use the same effective provider.
    _shared_subagent_cfg = None
    if request.user_agent is not None:
        from core.services.model_config import ModelConfigService

        _model_service = ModelConfigService.get_instance()
        _role_cfg = await asyncio.to_thread(_model_service.resolve, "subagent")
        if _role_cfg is not None:
            _shared_subagent_cfg = await asyncio.to_thread(
                _model_service.resolve_provider, _role_cfg.provider_id
            )
        if _shared_subagent_cfg is not None:
            request.model_provider_id = _shared_subagent_cfg.provider_id
            request.model_name = _shared_subagent_cfg.model_name

    inherited_run_binding = CURRENT_RUN_BINDING.get()
    if inherited_run_binding is not None:
        request.run_id = request.run_id or inherited_run_binding[0]
        request.journal_owner = request.journal_owner or inherited_run_binding[1]

    from core.capabilities.paths import capabilities_enabled

    _capability_run_key = request.run_id
    if capabilities_enabled():
        import uuid as _cap_uuid

        from core.capabilities import runtime as capability_runtime

        _capability_run_key = request.run_id or "ephemeral:" + _cap_uuid.uuid4().hex
        if request.user_agent is not None:
            request.user_agent = await asyncio.to_thread(
                capability_runtime.pin_agent_definition,
                _capability_run_key,
                str(request.current_user_id or ""),
                request.user_agent,
                scope_id=request.capability_scope,
            )

    import logging
    import time

    from core.llm.tool_permissions import resolve_approval_mode

    request.approval_mode = resolve_approval_mode(
        request.approval_mode, user_id=request.current_user_id
    )

    _log = logging.getLogger(__name__)
    _t0 = time.perf_counter_ns()
    _capability_notices = []

    def _note_unavailable(message):
        if message not in _capability_notices:
            _capability_notices.append(message)

    required.connector_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (request.required_mcp_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    required.skill_id = str(request.required_skill_id or "").strip()
    required.skill_name = str(request.required_skill_name or required.skill_id or "技能").strip()
    required.plugin_id = str(request.required_plugin_id or "").strip()
    required.plugin_name = str(request.required_plugin_name or required.plugin_id or "插件").strip()
    required.plugin_skill_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (request.required_plugin_skill_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    required.plugin_mcp_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (request.required_plugin_mcp_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    if required.plugin_id and not (required.plugin_skill_ids or required.plugin_mcp_ids):
        _note_unavailable(f"所选插件「{required.plugin_name}」当前没有可执行能力。")
    sticky.plugin_ids: List[str] = []
    sticky.plugin_skill_ids: List[str] = []
    sticky.plugin_mcp_ids: List[str] = []
    sticky.direct_skill_ids: List[str] = []
    sticky.direct_mcp_ids: List[str] = []

    def _elapsed():
        return f"{(time.perf_counter_ns() - _t0) / 1_000_000:.3f}ms"

    import asyncio

    return (
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
    )
