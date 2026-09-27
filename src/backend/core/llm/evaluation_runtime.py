"""Restricted runtime for reserved evaluation sessions.

The prefix only selects restrictions. Admission and every sandbox operation
must independently authorize the durable lease for the real account owner.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, is_dataclass, replace
from types import SimpleNamespace


@dataclass(frozen=True)
class EvaluationExecutionScope:
    owner_user_id: str
    session_id: str


CURRENT_EVALUATION_SCOPE: ContextVar[EvaluationExecutionScope | None] = ContextVar(
    "evaluation_tool_scope", default=None,
)


def is_evaluation_session(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith("eval_")


def parse_evaluation_scope(*, owner_user_id, chat_id, session_id=None):
    if not any(is_evaluation_session(value) for value in (chat_id, session_id)):
        return None
    target = session_id or chat_id
    if (
        not isinstance(target, str) or not re.fullmatch(r"eval_[0-9a-f]{32}", target)
        or not owner_user_id or (chat_id and chat_id != target)
    ):
        raise ValueError("Invalid or conflicting evaluation sandbox session")
    return EvaluationExecutionScope(str(owner_user_id), target)


def apply_evaluation_scope(context: dict) -> dict:
    scope = parse_evaluation_scope(
        owner_user_id=context.get("user_id"), chat_id=context.get("chat_id"),
        session_id=context.get("sandbox_session_id"),
    )
    if scope is None:
        return context
    if context.get("agent_api_scope"):
        raise ValueError("Evaluation sessions cannot inherit an agent API scope")
    result = dict(context)
    result.update(
        evaluation_session=True, sandbox_session_id=scope.session_id,
        memory_enabled=False, memory_write_enabled=False,
        memory_scope_user_id=scope.session_id, workspace_id=scope.session_id,
        channel_origin=None, project_id=None, project_name=None, project_instructions=None,
        project_files=None, project_folder_id=None, project_folder_kind=None,
        project_folder_name=None, project_local_path=None, project_is_local=False,
        plan_chat=False, batch_chat=False, workflow_chat=False, site_chat=False,
        ontology_enabled=False, ontology_runtime={}, mention_agent_id=None,
        explicit_subagent_command=None, skill_id=None, skill_name=None,
        plugin_id=None, plugin_name=None, connector_id=None,
    )
    for key in (
        "enabled_mcps", "enabled_mcp_ids", "enabled_skills", "enabled_skill_ids",
        "enabled_kbs", "enabled_kb_ids", "plugin_skill_ids", "plugin_mcp_ids",
        "connector_ids", "bridge_tools", "uploaded_files", "historical_files",
        "referenced_chats", "referenced_files", "skill_ids", "mcp_ids", "kb_ids",
    ):
        result[key] = []
    return result


def evaluation_user_agent(agent):
    """Keep the selected model/prompt, remove account capability grants."""
    if agent is None:
        return None
    changes = {key: [] for key in ("mcp_server_ids", "skill_ids", "kb_ids", "plugin_ids")}
    if is_dataclass(agent):
        return replace(agent, **changes)
    # Detached ORM instances must not share SQLAlchemy instrumentation with a
    # request-local capability projection.
    return SimpleNamespace(**{**{key: value for key, value in vars(agent).items()
                                if not key.startswith("_")}, **changes})
