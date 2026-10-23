"""One durable admission path for streaming and complete agent responses."""

import asyncio
from dataclasses import dataclass

import core.chat.context as chat_context
import core.chat.runtime.admission as admission
import core.db.engine as db_engine
import core.chat.service as chat_services_ChatService_module
import core.auth.users as chat_services_UserService_module
import core.chat.operations.admission_errors as chat_admission
import core.chat.operations.agent_targets as chat_agent_targets
import core.chat.operations.invocation as chat_invocation
import core.chat.operations.request_context as chat_request_context
import core.chat.operations.session_context as chat_session_context
from core.auth.backend import UserContext
from core.chat.schemas import ChatRequest
from core.infra.logging import get_logger, trace_id_var
from core.chat.sequencer import ChatBusyError, ChatSequencer
from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = get_logger(__name__)


@dataclass(frozen=True)
class StartedResponse:
    run_id: str
    message_id: str
    chat_id: str
    user_message_id: str


async def _read_preferences(request: ChatRequest, user_id: str):
    # Sessions cannot cross threads; each bounded read owns its connection.
    def capabilities():
        with db_engine.SessionLocal() as db:
            return chat_context.resolve_enabled_capabilities(
                db, user_id, request.enabled_skills, request.enabled_agents, request.enabled_mcps
            )

    def preferences():
        with db_engine.SessionLocal() as db:
            return chat_services_UserService_module.UserService(db).get_user_settings(user_id)

    return await asyncio.gather(asyncio.to_thread(capabilities), asyncio.to_thread(preferences))


def _check_project(db: Session, request: ChatRequest, user_id: str) -> None:
    """A chat bound to a project always runs in that project, whichever client sends to it."""
    from core.projects.access import require_visible_project

    if request.chat_id:
        pair = chat_services_ChatService_module.ChatService(db).get_session_with_access(request.chat_id, user_id)
        if pair is not None and pair[0].project_id:
            request.project_id = pair[0].project_id
    if request.project_id:
        require_visible_project(db, user_id, request.project_id)


def _link_attachments(db: Session, request: ChatRequest, user_id: str) -> None:
    ids = [item.file_id for item in request.attachments if item.file_id]
    if not ids:
        return
    from core.db.models import Artifact

    db.query(Artifact).filter(
        Artifact.artifact_id.in_(ids),
        Artifact.user_id == user_id,
        Artifact.chat_id.is_(None),
    ).update({"chat_id": request.chat_id}, synchronize_session="fetch")
    db.commit()


async def start_response_run(
    request: ChatRequest, user: UserContext, db: Session
) -> StartedResponse:
    from core.auth.agent_api import begin_agent_api_call, bind_agent_api_run, fail_agent_api_call, prepare_agent_api_request

    # Scope and API-session provenance come only from the authenticated key.
    # prepare logs its own rejected scoped requests before it raises.
    from core.projects.api import prepare_project_api_request

    request = prepare_project_api_request(db, user, request)
    request, scope = prepare_agent_api_request(db, user, request)
    if user.public_chat_visitor:
        from core.chat.public import validate_visitor_attachments

        validate_visitor_attachments(db, user, request.attachments)
    call_id = (
        begin_agent_api_call(db, scope, request.stream, trace_id=trace_id_var.get())
        if scope
        else None
    )
    try:
        chat_session_context.ensure_main_model_configured()
        user_id = chat_context.resolve_db_user_id(
            db, chat_session_context.authenticated_user_id(user)
        )
        from core.harness.registry.evaluation import authorize

        await authorize(request, user_id, agent_scoped=bool(scope))
        if scope:
            # Admission already verified this exact DB-backed agent. Never
            # enumerate the owner's catalog or parse delegation commands here.
            from core.db.models import UserAgent

            agent = db.get(UserAgent, scope["agent_id"])
            if agent is None:
                raise HTTPException(status_code=403, detail={"code": "agent_unavailable"})
            agent_name = agent.name
            request._resolved_agent_profile = "local"
            execution_message, command = request.message, None
        else:
            request, agent_name, execution_message, command = (
                chat_agent_targets._resolve_chat_agent_targets(db, request, user_id)
            )
        request = chat_invocation.resolve_explicit_capability_invocation(db, request, user_id)
        message = chat_context.build_effective_user_message(
            execution_message,
            request.quoted_follow_up,
            chat_request_context._resolve_reference_block(db, request, user_id),
        )
        provider_id = chat_session_context.resolve_selected_model_provider_id(db, request, user_id)
        model_name = chat_session_context.resolve_actual_chat_model_name(request, provider_id)
        (skills, agents, mcps), settings = await _read_preferences(request, user_id)
        if user.public_chat_visitor:
            from core.chat.public import visitor_approval_mode

            settings["tool_approval_mode"] = visitor_approval_mode(
                db, user, settings.get("tool_approval_mode")
            )
        _check_project(db, request, user_id)
        service = chat_services_ChatService_module.ChatService(db)
        chat_session_context._ensure_chat_session(
            service,
            request.chat_id,
            user_id,
            request.message,
            agent_id=request.agent_id,
            agent_name=agent_name,
            plan_chat=request.plan_chat,
            workflow_chat=request.workflow_chat,
            site_chat=request.site_chat,
            project_id=request.project_id,
        )
        context = chat_request_context.build_ctx(
            request,
            user_id,
            skills,
            agents,
            mcps,
            memory_enabled=bool(settings.get("memory_enabled", False)) and not scope,
            memory_write_enabled=bool(settings.get("memory_write_enabled", False)) and not scope,
            reranker_enabled=bool(settings.get("reranker_enabled", False)),
            model_provider_id=provider_id,
            actual_model_name=model_name,
            ontology_enabled=bool(settings.get("ontology_enabled", False)) and not scope,
            ontology_pack_ids=settings.get("ontology_pack_ids") or None,
            approval_mode=settings.get("tool_approval_mode"),
        )
        if user.api_key_project_id:
            from core.projects.api import make_project_api_scope

            context["project_api_scope"] = make_project_api_scope(user, request.chat_id)
        payload = request.model_dump(exclude_none=True)
        if user.api_key_project_id:
            payload["project_api_scope"] = make_project_api_scope(user, request.chat_id)
        # Authentication owns provenance; never accept a client-supplied origin.
        payload["request_origin"] = "api" if user.api_key_id else "cloud"
        if user.api_key_id:
            payload["api_key_id"] = str(user.api_key_id)
        if command:
            explicit_command = {
                "agent_id": command.agent_id,
                "agent_name": command.agent_name,
                "task": command.task,
            }
            context["explicit_subagent_command"] = explicit_command
            payload["explicit_subagent_command"] = explicit_command
        if provider_id:
            payload["model_provider_id"] = provider_id
        else:
            payload.pop("model_provider_id", None)
        if scope:
            context["agent_api_scope"] = dict(scope)
            payload["agent_api_scope"] = dict(scope)

        try:
            with ChatSequencer(db).launching_main_run(
                chat_id=request.chat_id,
                user_id=user_id,
                user_content=request.message,
                model=model_name,
                user_extra_data=chat_request_context._build_user_extra_data(request, provider_id),
                request_payload=payload,
            ) as accepted:
                if user.public_chat_visitor:
                    from core.db.models import ChatSession

                    session = db.get(ChatSession, request.chat_id)
                    if session and not (session.extra_data or {}).get("public_chat_title_set"):
                        session.title = (
                            request.message.strip().split("\n", 1)[0][:80] or session.title
                        )
                        session.extra_data = {
                            **(session.extra_data or {}),
                            "public_chat_title_set": True,
                        }
                        db.flush()
                # No history or attachment mutation before writer admission.
                messages = chat_session_context.load_session_messages(
                    service, request.chat_id, user_id
                )
                from core.chat.plan_progress import clear_plan_progress

                clear_plan_progress(request.chat_id)
                _link_attachments(db, request, user_id)
                if call_id:
                    bind_agent_api_run(db, call_id, accepted.run.run_id)
                run = await admission.start_run(
                    accepted_run=accepted.run,
                    chat_id=request.chat_id,
                    user_id=user_id,
                    session_messages=messages,
                    effective_user_message=message,
                    raw_user_message=request.message,
                    context=context,
                    model_name=model_name,
                )
        except ChatBusyError as exc:
            raise chat_admission.chat_busy_http_exception(exc) from exc
        # Title work is auxiliary: it starts after first-message admission and
        # never waits for the agent answer or blocks a successfully launched run.
        from core.chat.titles import schedule_first_title

        try:
            schedule_first_title(request.chat_id, user_id, run.run_id)
        except Exception as title_error:
            logger.warning("chat_title_schedule_failed: %s", type(title_error).__name__)
        return StartedResponse(run.run_id, run.message_id, request.chat_id, accepted.user_message.message_id)
    except Exception as exc:
        if call_id:
            status = exc.status_code if isinstance(exc, HTTPException) else 500
            # Logs carry a bounded machine code, never raw model/tool errors or secrets.
            detail = exc.detail if isinstance(exc, HTTPException) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            fail_agent_api_call(db, call_id, status, code or "request_rejected")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("agent_response_start_failed", chat_id=request.chat_id)
        raise HTTPException(
            status_code=500, detail=chat_context.resolve_user_facing_error(exc)
        ) from exc
    finally:
        # Both HTTP transports must release the request transaction before
        # waiting on a worker that writes the same chat rows.
        chat_session_context.release_request_session(db)
