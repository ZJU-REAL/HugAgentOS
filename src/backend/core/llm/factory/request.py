"""Per-call agent assembly options, normalized before tool construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from orchestration.registry import AgentSpec


@dataclass
class AgentRequest:
    agent_spec: Optional[AgentSpec] = None
    user_query: Optional[str] = None
    disable_tools: bool = False
    enabled_skill_ids: Optional[list[str]] = None
    enabled_mcp_ids: Optional[list[str]] = None
    enabled_kb_ids: Optional[list[str]] = None
    current_user_id: Optional[str] = None
    reranker_enabled: bool = False
    model_name: Optional[str] = None
    model_provider_id: Optional[str] = None
    chat_mode: Optional[str] = None
    memory_enabled: bool = False
    user_agent: Optional[Any] = None
    visible_subagents: Optional[List[Dict[str, Any]]] = None
    isolated: bool = False
    max_iters: Optional[int] = None
    plan_mode: bool = False
    model_role: Optional[str] = None
    batch_mode: bool = False
    workflow_mode: bool = False
    top_level_chat: bool = False
    chat_id: Optional[str] = None
    run_id: Optional[str] = None
    journal_owner: Optional[str] = None
    capability_scope: str = ""
    workspace_id: str = "default"
    sandbox_session_id: Optional[str] = None
    project_ctx: Optional[Dict[str, Any]] = None
    channel_origin: Optional[Dict[str, Any]] = None
    automation_run: bool = False
    read_only: bool = False
    allow_bash: bool = True
    approval_mode: Optional[str] = None
    turbo_mode: bool = False
    turbo_explicit_skill_ids: Optional[List[str]] = None
    turbo_explicit_mcp_ids: Optional[List[str]] = None
    invoked_skill_ids: Optional[List[str]] = None
    invoked_mcp_ids: Optional[List[str]] = None
    required_mcp_ids: Optional[List[str]] = None
    required_skill_id: Optional[str] = None
    required_skill_name: Optional[str] = None
    required_plugin_id: Optional[str] = None
    required_plugin_name: Optional[str] = None
    required_plugin_skill_ids: Optional[List[str]] = None
    required_plugin_mcp_ids: Optional[List[str]] = None
    mode_spec: Optional[Any] = None
    ontology_runtime: Optional[Dict[str, Any]] = None
    tool_result_limit: Optional[int] = None
    agent_api_scope: Optional[Dict[str, Any]] = None
