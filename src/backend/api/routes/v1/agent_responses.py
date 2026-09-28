"""One durable admission path for streaming and complete agent responses."""

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.routes.v1 import chats
from api.schemas import ChatRequest, ChatResponse
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.logging import get_logger, trace_id_var
from core.infra.responses import sse_response
from core.services.chat_sequencer import ChatBusyError, ChatSequencer
from orchestration import chat_run_executor

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["AgentResponses"])


@dataclass(frozen=True)
class StartedResponse:
    run_id: str
    message_id: str
    chat_id: str


async def _read_preferences(request: ChatRequest, user_id: str):
    # Sessions cannot cross threads; each bounded read owns its connection.
    def capabilities():
        with chats.SessionLocal() as db:
            return chats.resolve_enabled_capabilities(
                db, user_id, request.enabled_skills, request.enabled_agents, request.enabled_mcps
            )

    def preferences():
        with chats.SessionLocal() as db:
            return chats.UserService(db).get_user_settings(user_id)

    return await asyncio.gather(
        asyncio.to_thread(capabilities), asyncio.to_thread(preferences)
    )


def _check_project(db: Session, request: ChatRequest, user_id: str) -> None:
    if not request.project_id:
        return
    from core.auth.permissions_iface import resolve_project_permission
    from core.db.models import Project

    project = db.query(Project).filter(
        Project.project_id == request.project_id, Project.deleted_at.is_(None)
    ).first()
    if project is None or resolve_project_permission(db, user_id, project) == "none":
        raise HTTPException(status_code=404, detail="项目不存在或你无权访问")


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


async def _start_response_run(
    request: ChatRequest, user: UserContext, db: Session
) -> StartedResponse:
    from core.services.agent_api_service import (
        begin_agent_api_call, bind_agent_api_run, fail_agent_api_call,
        prepare_agent_api_request,
    )

    # Scope and API-session provenance come only from the authenticated key.
    # prepare logs its own rejected scoped requests before it raises.
    request, scope = prepare_agent_api_request(db, user, request)
    call_id = (
        begin_agent_api_call(db, scope, request.stream, trace_id=trace_id_var.get())
        if scope else None
    )
    accepted = None
    started = False
    try:
        chats._ensure_main_model_configured()
        user_id = chats.resolve_db_user_id(db, chats._authenticated_user_id(user))
        from core.services.evaluation_admission import authorize
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
            request, agent_name, execution_message, command = chats._resolve_chat_agent_targets(
                db, request, user_id
            )
        request = chats._resolve_explicit_capability_invocation(db, request, user_id)
        message = chats._build_effective_user_message(
            execution_message, request.quoted_follow_up,
            chats._resolve_reference_block(db, request, user_id),
        )
        provider_id = chats._resolve_selected_model_provider_id(db, request, user_id)
        model_name = chats._resolve_actual_chat_model_name(request, provider_id)
        (skills, agents, mcps), settings = await _read_preferences(request, user_id)
        _check_project(db, request, user_id)
        service = chats.ChatService(db)
        chats._ensure_chat_session(
            service, request.chat_id, user_id, request.message,
            agent_id=request.agent_id, agent_name=agent_name,
            plan_chat=request.plan_chat, batch_chat=request.batch_chat,
            workflow_chat=request.workflow_chat, site_chat=request.site_chat,
            project_id=request.project_id,
        )
        context = chats._build_ctx(
            request, user_id, skills, agents, mcps,
            memory_enabled=bool(settings.get("memory_enabled", False)) and not scope,
            memory_write_enabled=bool(settings.get("memory_write_enabled", False)) and not scope,
            reranker_enabled=bool(settings.get("reranker_enabled", False)),
            model_provider_id=provider_id, actual_model_name=model_name,
            ontology_enabled=bool(settings.get("ontology_enabled", False)) and not scope,
            ontology_pack_ids=settings.get("ontology_pack_ids") or None,
            approval_mode=settings.get("tool_approval_mode"),
        )
        payload = request.model_dump(exclude_none=True)
        if command:
            explicit_command = {
                "agent_id": command.agent_id, "agent_name": command.agent_name,
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
            accepted = ChatSequencer(db).accept_main_run(
                chat_id=request.chat_id, user_id=user_id, user_content=request.message,
                model=model_name,
                user_extra_data=chats._build_user_extra_data(request, provider_id),
                request_payload=payload,
            )
        except ChatBusyError as exc:
            raise chats.chat_busy_http_exception(exc) from exc

        # No history or attachment mutation is allowed before writer admission.
        messages = chats._load_session_messages(service, request.chat_id, user_id)
        from core.chat.plan_progress import clear_plan_progress
        clear_plan_progress(request.chat_id)
        _link_attachments(db, request, user_id)
        if call_id:
            bind_agent_api_run(db, call_id, accepted.run.run_id)
        run = await chat_run_executor.start_run(
            accepted_run=accepted.run, chat_id=request.chat_id, user_id=user_id,
            session_messages=messages, effective_user_message=message,
            raw_user_message=request.message, context=context, model_name=model_name,
        )
        result = StartedResponse(run.run_id, run.message_id, request.chat_id)
        started = True
        return result
    except Exception as exc:
        if accepted is not None and not started:
            ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
        if call_id:
            status = exc.status_code if isinstance(exc, HTTPException) else 500
            # Logs carry a bounded machine code, never raw model/tool errors or secrets.
            detail = exc.detail if isinstance(exc, HTTPException) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            fail_agent_api_call(db, call_id, status, code or "request_rejected")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("agent_response_start_failed", chat_id=request.chat_id)
        raise HTTPException(status_code=500, detail=chats.resolve_user_facing_error(exc)) from exc
    finally:
        # Both HTTP transports must release the request transaction before
        # waiting on a worker that writes the same chat rows.
        chats._release_request_session(db)


async def _wait_response(run_id: str, *, chat_id: str, message_id: str) -> ChatResponse:
    terminal = await chat_run_executor.wait_run(run_id)
    if terminal.status == "cancelled":
        raise HTTPException(status_code=409, detail={"code": "run_cancelled", "run_id": run_id})
    if terminal.status == "needs_attention":
        raise HTTPException(
            status_code=409, detail={"code": "run_needs_attention", "run_id": run_id}
        )
    if terminal.status != "completed":
        raise HTTPException(
            status_code=500,
            detail=chats.resolve_user_facing_error(
                RuntimeError(terminal.error_message or "chat run failed")
            ),
        )
    with chats.SessionLocal() as db:
        assistant = chats.ChatService(db).get_message_by_id(message_id)
        if assistant is None:
            raise HTTPException(status_code=500, detail="已完成的任务缺少回复")
        meta = assistant.extra_data or {}
        return ChatResponse(
            chat_id=chat_id, response=assistant.content, timestamp=chats.now_iso(),
            is_markdown=bool(meta.get("is_markdown", False)),
            route=meta.get("route", "main"), sources=meta.get("sources", []),
            artifacts=meta.get("artifacts", []), warnings=meta.get("warnings", []),
        )


@router.post(
    "/responses",
    summary="生成智能体回复（JSON / SSE）",
    response_model=ChatResponse,
    responses={
        200: {
            "description": "stream=false 返回 ChatResponse；stream=true 返回 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
)
async def agent_response(
    request: ChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """同一请求通过 stream 选择完整 JSON 或 SSE；两者共享后台 Run 和权限校验。

    子智能体请求须显式传入 agent_id；专属 API Key 只能调用绑定的目标和 API 会话。SSE 断线可通过
    GET /v1/chats/stream/{run_id}?from=N 续播；HTTP 断开不会取消后台任务。
    """
    run = await _start_response_run(request, user, db)
    if request.stream:
        return sse_response(chat_run_executor.follow_run_as_sse(run.run_id, chat_id=run.chat_id))
    try:
        result = await _wait_response(run.run_id, chat_id=run.chat_id, message_id=run.message_id)
    except HTTPException as exc:
        await _record_http_status(user, run.run_id, exc.status_code)
        raise
    await _record_http_status(user, run.run_id, 200)
    return result


async def _record_http_status(user: UserContext, run_id: str, status_code: int) -> None:
    if user.api_key_agent_id:
        from core.services.agent_api_service import record_agent_api_http_status

        await asyncio.to_thread(record_agent_api_http_status, run_id, status_code)
