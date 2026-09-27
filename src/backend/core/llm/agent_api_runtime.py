"""Runtime boundary for an authenticated, agent-scoped API request.

Configuration and billing belong to the issuer. Private execution data does not:
API runs get a separate sandbox principal and may only read their own artifacts.
The scope is reconstructed from trusted durable context, never model arguments.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AgentApiExecutionScope:
    owner_user_id: str
    api_key_id: str
    agent_id: str
    chat_id: str
    sandbox_user_id: str
    sandbox_session_id: str


def parse_api_scope(
    value: Mapping[str, Any] | None, *, owner_user_id: str,
    chat_id: str | None, agent_id: str | None,
) -> AgentApiExecutionScope | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Invalid agent API execution scope")
    fields = tuple(AgentApiExecutionScope.__dataclass_fields__)
    if any(not isinstance(value.get(name), str) or not value[name] for name in fields):
        raise ValueError("Incomplete agent API execution scope")
    scope = AgentApiExecutionScope(**{name: value[name] for name in fields})
    if (scope.owner_user_id, scope.chat_id, scope.agent_id) != (
        str(owner_user_id), str(chat_id), str(agent_id),
    ):
        raise ValueError("Agent API execution target does not match its scope")
    if scope.sandbox_user_id != f"api_{scope.api_key_id}":
        raise ValueError("Agent API sandbox cannot use the account identity")
    expected_session = "api_" + hashlib.sha256(
        f"{scope.api_key_id}:{scope.chat_id}".encode()
    ).hexdigest()[:40]
    if scope.sandbox_session_id != expected_session:
        raise ValueError("Agent API sandbox session does not belong to this chat")
    return scope


def apply_api_scope(context: dict) -> dict:
    scope = parse_api_scope(
        context.get("agent_api_scope"), owner_user_id=str(context.get("user_id") or ""),
        chat_id=context.get("chat_id"), agent_id=context.get("direct_agent_id") or context.get("agent_id"),
    )
    if scope is None:
        return context
    result = dict(context)
    result.update(
        agent_id=scope.agent_id, direct_agent_id=scope.agent_id, direct_agent_source="agent_api",
        memory_enabled=False, memory_write_enabled=False,
        memory_scope_user_id=scope.sandbox_user_id,
        workspace_id=f"agent-api:{scope.api_key_id}",
        sandbox_session_id=scope.sandbox_session_id,
        visible_subagents=[], enabled_agents=[], mention_agent_id=None,
        explicit_subagent_command=None, channel_origin=None, project_id=None,
        project_name=None, project_instructions=None, project_files=None,
        project_folder_id=None, project_folder_kind=None, project_folder_name=None,
        project_local_path=None, project_is_local=False,
        plan_chat=False, batch_chat=False, workflow_chat=False, site_chat=False,
        ontology_enabled=False, ontology_runtime={},
    )
    return result


def scope_mcp_servers(servers: dict, scope: AgentApiExecutionScope) -> dict:
    """Use per-request HTTP clients; never launch owner-configured host commands.

    The KB service receives only the already-authorized KB id set. Other MCPs
    receive the API principal instead of an account identifier. Explicit service
    credentials remain the publisher's resource grant.
    """
    out = {}
    for name, config in servers.items():
        if not isinstance(config, dict) or not config.get("url"):
            raise ValueError(f"API 模式不支持在后端主机执行 stdio MCP：{name}")
        item = dict(config)
        headers = {
            key: val for key, val in (item.get("headers") or {}).items()
            if key.lower() not in {
                "x-current-user-id", "x-chat-id", "x-channel-id", "x-conversation-id",
            }
        }
        headers.update({
            "X-Current-User-Id": scope.sandbox_user_id,
            "X-Chat-Id": scope.chat_id,
            "X-Agent-Api-Key-Id": scope.api_key_id,
        })
        # Empty allow-lists mean "all accessible" to legacy KB services.
        # A non-existent sentinel preserves an explicitly empty resource grant.
        for header in ("X-Allowed-Kb-Ids", "X-Allowed-Dataset-Ids"):
            existing = next((key for key in headers if key.lower() == header.lower()), header)
            if not str(headers.get(existing) or "").strip():
                headers[existing] = "__agent_api_no_kb__"
        item["headers"] = headers
        out[name] = item
    return out


def artifact_in_scope(file_id: str, scope: AgentApiExecutionScope) -> bool:
    """Authorize before any parser or local-project fallback is touched."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(file_id or "")):
        return False
    from core.db.engine import SessionLocal
    from core.db.models import Artifact, ChatSession
    from core.services.agent_api_service import session_matches_scope
    with SessionLocal() as db:
        session = db.get(ChatSession, scope.chat_id)
        if not session or not session_matches_scope(session, {"version": 1, **asdict(scope)}):
            return False
        artifact = db.get(Artifact, file_id)
        if artifact is not None:
            return (
                artifact.user_id == scope.owner_user_id
                and artifact.chat_id == scope.chat_id
                and artifact.deleted_at is None
            )
    # An output is in the file index before pinning/finalization persists it.
    # Read only index metadata here; get_artifact resolves host project paths.
    from core.artifacts.store import _read_record
    item = _read_record(file_id) if file_id else None
    metadata = (item or {}).get("metadata") or {}
    return (
        metadata.get("source") == "sandbox_get_artifact"
        and metadata.get("user_id") == scope.owner_user_id
        and metadata.get("chat_id") == scope.chat_id
        and metadata.get("agent_api_key_id") == scope.api_key_id
    )
