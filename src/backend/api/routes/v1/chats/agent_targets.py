"""Resolve persistent agents, per-turn mentions and rerun identities."""

from typing import Any, Optional

import core.services.user_service as user_service
from api.schemas import ChatRequest
from fastapi import HTTPException
from sqlalchemy.orm import Session


def _resolve_chat_agent_targets(
    db: Session,
    request: ChatRequest,
    user_id: str,
) -> tuple[ChatRequest, Optional[str], str, Optional[Any]]:
    """Resolve persistent and per-turn direct sub-agent targets.

    ``agent_id`` binds a dedicated sub-agent conversation and is therefore
    persisted on the chat session. ``mention_agent_id`` is an explicit @mention
    delegation for this turn only. Older clients send only ``mention_name``;
    accept that form when it resolves to exactly one accessible agent. A
    personal disabled flag suppresses autonomous discovery, not an explicit
    target selected for this turn.

    A strict natural-language command (``调用「完整名称」子智能体：...``) is
    returned separately as ``explicit_command``. It must not be rewritten to
    ``mention_agent_id``. Both forms remain on the main-model stream and
    constrain its next real tool call to ``call_subagent``. Only a persistent
    ``agent_id`` conversation executes the selected sub-agent directly.
    """
    from core.llm.builtin_subagents import get_builtin_subagent, merge_builtin_subagents
    from core.services.project_init import resolve_project_init
    from core.services.user_agent_service import UserAgentService

    initialized = resolve_project_init(db, request, user_id)
    if initialized is not None:
        return request, None, initialized, None

    from core.services.subagent_routing_service import may_be_explicit_subagent_command

    if not (
        request.agent_id
        or request.mention_agent_id
        or request.mention_name
        or may_be_explicit_subagent_command(request.message)
    ):
        return request, None, request.message, None

    service = UserAgentService(db)
    disabled_ids = user_service.UserService(db).get_disabled_builtin_subagent_ids(user_id)
    explicitly_callable_delegates = merge_builtin_subagents(
        service.list_for_user(user_id),
        disabled_agent_ids=disabled_ids,
        include_disabled=True,
    )
    persistent_agent_name: Optional[str] = None
    execution_message = request.message
    explicit_command = None

    if request.agent_id:
        persistent = next(
            (
                item
                for item in explicitly_callable_delegates
                if item.get("agent_id") == request.agent_id
            ),
            None,
        )
        if persistent is None:
            raise HTTPException(status_code=403, detail="无法访问该子智能体")
        persistent_agent_name = str(persistent["name"])
        request._resolved_agent_profile = str(persistent.get("profile") or "local")

    mention_agent_id = request.mention_agent_id
    mention_agent_name = request.mention_name
    if not mention_agent_id and not mention_agent_name:
        from core.services.subagent_routing_service import parse_explicit_subagent_command

        explicit_command = parse_explicit_subagent_command(
            request.message,
            explicitly_callable_delegates,
        )
        if explicit_command:
            return request, persistent_agent_name, explicit_command.task, explicit_command

    if mention_agent_id:
        builtin = get_builtin_subagent(mention_agent_id)
        if builtin is not None:
            mentioned = next(
                (
                    item
                    for item in explicitly_callable_delegates
                    if item.get("agent_id") == mention_agent_id
                ),
                None,
            )
            if mentioned is None:
                raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体")
        else:
            try:
                mentioned = service.get_by_id(mention_agent_id, user_id=user_id)
            except (LookupError, PermissionError) as exc:
                raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体") from exc
        if mention_agent_name:
            selected_display_name = mention_agent_name
            mention_agent_name = str(mentioned["name"])
            execution_message = _strip_direct_mention_prefix(
                request.message,
                selected_display_name,
            )
        else:
            from core.services.subagent_routing_service import parse_explicit_subagent_command

            command = parse_explicit_subagent_command(request.message, [mentioned])
            if command:
                execution_message = command.task
    elif mention_agent_name:
        exact_matches = [
            item for item in explicitly_callable_delegates if item.get("name") == mention_agent_name
        ]
        if not exact_matches:
            raise HTTPException(status_code=403, detail="无法访问 @ 指定的子智能体")
        if len(exact_matches) > 1:
            raise HTTPException(
                status_code=409,
                detail="存在同名子智能体，请重新从 @ 列表选择以确定目标",
            )
        mention_agent_id = str(exact_matches[0]["agent_id"])
        execution_message = _strip_direct_mention_prefix(
            request.message,
            mention_agent_name,
        )

    if mention_agent_id != request.mention_agent_id or (
        mention_agent_name and mention_agent_name != request.mention_name
    ):
        request = request.model_copy(
            update={
                "mention_agent_id": mention_agent_id,
                "mention_name": mention_agent_name,
            }
        )

    if mention_agent_id:
        target = next(
            (
                item
                for item in explicitly_callable_delegates
                if str(item.get("agent_id")) == str(mention_agent_id)
            ),
            None,
        )
        if target is not None:
            request._resolved_mention_agent_profile = str(target.get("profile") or "local")
    return request, persistent_agent_name, execution_message, explicit_command


def _strip_direct_mention_prefix(message: str, mention_name: Optional[str]) -> str:
    """Remove the frontend's display-only ``@name`` prefix before execution."""
    if not mention_name:
        return message
    token = f"@{mention_name}"
    if message == token:
        return message
    if message.startswith(token) and len(message) > len(token):
        separator = message[len(token)]
        if separator.isspace():
            stripped = message[len(token) :].lstrip()
            return stripped or message
    return message


def _saved_agent_source_profile(
    db, *, chat_id, user_id, agent_id, user_message_id, assistant_message_id=None
):
    """Read only the identity from a proven old run; never reuse its definition."""
    import hashlib

    from core.db.models import ChatRun, ContentBlock

    query = db.query(ChatRun).filter(ChatRun.chat_id == chat_id, ChatRun.user_id == user_id)
    if assistant_message_id:
        query = query.filter(ChatRun.message_id == assistant_message_id)
    else:
        query = query.filter(ChatRun.user_message_id == user_message_id)
    for run in query.order_by(ChatRun.created_at.desc()).limit(32):
        key = (
            "desktop_capability_agent:"
            + hashlib.sha256((str(run.run_id) + ":" + str(agent_id)).encode()).hexdigest()
        )
        row = db.get(ContentBlock, key)
        if row is None:
            continue
        saved = row.payload or {}
        definition = saved.get("definition") or {}
        if (
            saved.get("run_id") != run.run_id
            or saved.get("user_id") != user_id
            or definition.get("agent_id") != agent_id
        ):
            continue
        profile = saved.get("profile") or definition.get("profile") or "local"
        if definition.get("profile", profile) == profile:
            return str(profile)
    return None


def _resolve_rerun_agent_targets(
    db, request, user_id, session, user_message, *, assistant_message_id=None
):
    """Restore legacy dedicated chats and recheck each saved source before admission."""
    extra = user_message.extra_data or {}
    if not request.agent_id:
        agent_id = (session.extra_data or {}).get("agent_id")
        if agent_id:
            request = request.model_copy(update={"agent_id": str(agent_id)})
    request, name, execution_message, command = _resolve_chat_agent_targets(db, request, user_id)
    for id_field, profile_field, resolved_field in (
        ("agent_id", "agent_profile", "_resolved_agent_profile"),
        ("mention_agent_id", "mention_agent_profile", "_resolved_mention_agent_profile"),
    ):
        agent_id = getattr(request, id_field, None)
        if not agent_id:
            continue
        expected = extra.get(profile_field)
        if not expected:
            expected = _saved_agent_source_profile(
                db,
                chat_id=request.chat_id,
                user_id=user_id,
                agent_id=agent_id,
                user_message_id=user_message.message_id,
                assistant_message_id=assistant_message_id,
            )
        current = getattr(request, resolved_field, None)
        if current and current not in ("local", "builtin") and not expected:
            raise HTTPException(
                status_code=409, detail="无法确认历史云端子智能体的来源，请重新选择该子智能体后发送"
            )
        if expected and current != expected:
            raise HTTPException(
                status_code=403,
                detail="历史子智能体的来源账号已变化，请重新选择；未切换到同名智能体",
            )
    return request, name, execution_message, command
