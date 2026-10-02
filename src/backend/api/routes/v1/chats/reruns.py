"""Regenerate and edit endpoints sharing durable-run startup and failure cleanup."""

from typing import Any, Dict

import api.routes.v1.chat_admission as chat_admission
import api.routes.v1.chats.agent_targets as chat_agent_targets
import api.routes.v1.chats.invocation as chat_invocation
import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.request_context as chat_request_context
import api.routes.v1.chats.rerun_metadata as chat_rerun_metadata
import api.routes.v1.chats.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.infra.responses as responses
import core.services as chat_services
from api.schemas import ChatRequest
from core.auth.backend import UserContext
from core.infra.exceptions import ResourceNotFoundError
from core.services.chat_reference_service import render_reference_block
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

router = APIRouter()


@router.post("/{chat_id}/regenerate", summary="重新生成助手回复 (SSE)")
async def regenerate_message(
    chat_id: str,
    body: chat_models.RegenerateRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Delete the target assistant message and all subsequent, then re-stream."""

    def prepare():
        chat_session_context._ensure_main_model_configured()
        chat_service = chat_services.ChatService(db)
        db_user_id = chat_context.resolve_db_user_id(
            db, chat_session_context._authenticated_user_id(user)
        )

        # Shared sessions: deleting/rewriting history is only granted to the session owner / project admin (the "admin" level in the shared context).
        pair = chat_service.get_session_with_access(chat_id, db_user_id)
        if pair is None:
            raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
        _sess, _level = pair
        if _level != "admin":
            raise HTTPException(status_code=403, detail="共享会话仅创建者可重新生成回复")

        target_msg = chat_service.get_message_by_index(chat_id, body.message_index)
        if not target_msg or target_msg.chat_id != chat_id:
            raise HTTPException(status_code=404, detail="消息不存在")

        user_msg = chat_service.get_user_message_before(chat_id, target_msg.message_id)
        if not user_msg:
            raise HTTPException(status_code=400, detail="找不到对应的用户消息")

        user_content = user_msg.content
        user_extra = user_msg.extra_data or {}
        attachment_items = chat_rerun_metadata._restore_attachments(
            user_extra.get("attachments", [])
        )

        regen_request = ChatRequest(
            chat_id=chat_id,
            project_id=_sess.project_id,
            site_chat=bool((_sess.extra_data or {}).get("site_chat")),
            message=user_content,
            model_name="qwen",
            enable_thinking=user_extra.get("enable_thinking", False),
            quoted_follow_up=user_extra.get("quoted_follow_up"),
            attachments=attachment_items,
            model_provider_id=user_extra.get("model_provider_id"),
            **chat_rerun_metadata._restore_invocation(user_extra),
        )
        regen_request, _, execution_message, explicit_subagent_command = (
            chat_agent_targets._resolve_rerun_agent_targets(
                db,
                regen_request,
                db_user_id,
                _sess,
                user_msg,
                assistant_message_id=target_msg.message_id,
            )
        )
        regen_request = chat_invocation._resolve_explicit_capability_invocation(
            db, regen_request, db_user_id
        )
        from core.services.project_init import resolve_project_init

        init_message = resolve_project_init(db, regen_request, db_user_id)
        selected_model_provider_id = chat_session_context._resolve_selected_model_provider_id(
            db, regen_request, db_user_id
        )
        actual_model_name = chat_session_context._resolve_actual_chat_model_name(
            regen_request,
            selected_model_provider_id,
        )
        enabled_skills, enabled_agents, enabled_mcps = chat_context.resolve_enabled_capabilities(
            db, db_user_id
        )
        _user_settings = chat_services.UserService(db).get_user_settings(db_user_id)
        effective_msg = chat_context.build_effective_user_message(
            init_message or execution_message,
            regen_request.quoted_follow_up,
            render_reference_block(user_extra.get("referenced_chats")),
        )

        context = chat_request_context._build_ctx(
            regen_request,
            db_user_id,
            enabled_skills,
            enabled_agents,
            enabled_mcps,
            memory_enabled=bool(_user_settings.get("memory_enabled", False)),
            memory_write_enabled=bool(_user_settings.get("memory_write_enabled", False)),
            reranker_enabled=bool(_user_settings.get("reranker_enabled", False)),
            model_provider_id=selected_model_provider_id,
            actual_model_name=actual_model_name,
            ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
            ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
            approval_mode=_user_settings.get("tool_approval_mode"),
        )

        from core.services.chat_sequencer import ChatBusyError, ChatSequencer

        request_payload = regen_request.model_dump(exclude_none=True)
        request_payload["operation"] = "regenerate"
        if explicit_subagent_command:
            command = {
                "agent_id": explicit_subagent_command.agent_id,
                "agent_name": explicit_subagent_command.agent_name,
                "task": explicit_subagent_command.task,
            }
            request_payload["explicit_subagent_command"] = command
            context["explicit_subagent_command"] = command
        try:
            accepted = ChatSequencer(db).accept_existing_user_run(
                chat_id=chat_id,
                user_id=db_user_id,
                user_message_id=user_msg.message_id,
                delete_from_message_id=target_msg.message_id,
                delete_from_chat_seq=target_msg.chat_seq,
                request_payload=request_payload,
            )
        except ChatBusyError as exc:
            raise chat_admission.chat_busy_http_exception(exc) from exc

        try:
            session_messages = chat_session_context._load_session_messages(
                chat_service, chat_id, db_user_id
            )
            kwargs = dict(
                accepted_run=accepted.run,
                chat_id=chat_id,
                user_id=db_user_id,
                session_messages=session_messages,
                effective_user_message=effective_msg,
                raw_user_message=user_content,
                context=context,
                model_name=actual_model_name,
            )
            if hasattr(accepted.run, "_sa_instance_state"):
                db.refresh(accepted.run)
                db.expunge(accepted.run)
            return kwargs
        except Exception as exc:
            ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
            raise

    return await _start_prepared_run(prepare, db, chat_id)


@router.post("/{chat_id}/edit", summary="编辑消息并重新生成 (SSE)")
async def edit_and_resend(
    chat_id: str,
    body: chat_models.EditAndResendRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Delete the target user message and all subsequent, then re-stream with new content."""

    def prepare():
        chat_session_context._ensure_main_model_configured()
        chat_service = chat_services.ChatService(db)
        db_user_id = chat_context.resolve_db_user_id(
            db, chat_session_context._authenticated_user_id(user)
        )

        # Shared sessions: editing history is an owner-only operation (rewriting others' / one's own earlier messages would break the collaboration context).
        pair = chat_service.get_session_with_access(chat_id, db_user_id)
        if pair is None:
            raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
        _sess, _level = pair
        if _level != "admin":
            raise HTTPException(status_code=403, detail="共享会话仅创建者可编辑历史消息")

        target_msg = chat_service.get_message_by_index(chat_id, body.message_index)
        if not target_msg or target_msg.chat_id != chat_id or target_msg.role != "user":
            raise HTTPException(status_code=404, detail="用户消息不存在")

        target_extra = target_msg.extra_data or {}
        saved_attachments = target_extra.get("attachments", [])
        attachment_items = chat_rerun_metadata._restore_attachments(saved_attachments)
        # Editing rewrites the text only: the files the user uploaded and the
        # skill / plugin / connector / @agent this turn referenced are carried over.
        saved_invocation = chat_rerun_metadata._restore_invocation(target_extra)
        saved_quoted_follow_up = target_extra.get("quoted_follow_up")
        saved_reference_cards = target_extra.get("referenced_chats")

        edit_request = ChatRequest(
            chat_id=chat_id,
            project_id=_sess.project_id,
            site_chat=bool((_sess.extra_data or {}).get("site_chat")),
            message=body.new_content,
            model_name="qwen",
            attachments=attachment_items,
            quoted_follow_up=saved_quoted_follow_up,
            model_provider_id=target_extra.get("model_provider_id"),
            **saved_invocation,
        )
        edit_request, _, execution_message, explicit_subagent_command = (
            chat_agent_targets._resolve_rerun_agent_targets(
                db,
                edit_request,
                db_user_id,
                _sess,
                target_msg,
            )
        )
        from core.services.project_init import resolve_project_init

        init_message = resolve_project_init(db, edit_request, db_user_id)
        edit_request = chat_invocation._resolve_explicit_capability_invocation(
            db, edit_request, db_user_id
        )
        selected_model_provider_id = chat_session_context._resolve_selected_model_provider_id(
            db, edit_request, db_user_id
        )
        actual_model_name = chat_session_context._resolve_actual_chat_model_name(
            edit_request, selected_model_provider_id
        )
        enabled_skills, enabled_agents, enabled_mcps = chat_context.resolve_enabled_capabilities(
            db, db_user_id
        )
        _user_settings = chat_services.UserService(db).get_user_settings(db_user_id)

        # Persist the edited user message
        _edit_extra: Dict[str, Any] = {"timestamp": chat_context.now_iso(), **saved_invocation}
        if edit_request.agent_id:
            _edit_extra["agent_id"] = edit_request.agent_id
            if getattr(edit_request, "_resolved_agent_profile", None):
                _edit_extra["agent_profile"] = edit_request._resolved_agent_profile
        if getattr(edit_request, "_resolved_mention_agent_profile", None):
            _edit_extra["mention_agent_profile"] = edit_request._resolved_mention_agent_profile
        if saved_attachments:
            _edit_extra["attachments"] = saved_attachments
        if saved_quoted_follow_up:
            _edit_extra["quoted_follow_up"] = saved_quoted_follow_up
        if saved_reference_cards:
            _edit_extra["referenced_chats"] = saved_reference_cards
        if selected_model_provider_id:
            _edit_extra["model_provider_id"] = selected_model_provider_id

        context = chat_request_context._build_ctx(
            edit_request,
            db_user_id,
            enabled_skills,
            enabled_agents,
            enabled_mcps,
            memory_enabled=bool(_user_settings.get("memory_enabled", False)),
            memory_write_enabled=bool(_user_settings.get("memory_write_enabled", False)),
            reranker_enabled=bool(_user_settings.get("reranker_enabled", False)),
            model_provider_id=selected_model_provider_id,
            actual_model_name=actual_model_name,
            ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
            ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
            approval_mode=_user_settings.get("tool_approval_mode"),
        )

        from core.services.chat_sequencer import ChatBusyError, ChatSequencer

        request_payload = edit_request.model_dump(exclude_none=True)
        request_payload["operation"] = "edit"
        if explicit_subagent_command:
            command = {
                "agent_id": explicit_subagent_command.agent_id,
                "agent_name": explicit_subagent_command.agent_name,
                "task": explicit_subagent_command.task,
            }
            request_payload["explicit_subagent_command"] = command
            context["explicit_subagent_command"] = command
        try:
            accepted = ChatSequencer(db).accept_replacement_user_run(
                chat_id=chat_id,
                user_id=db_user_id,
                target_user_message_id=target_msg.message_id,
                delete_from_chat_seq=target_msg.chat_seq,
                user_content=body.new_content,
                request_payload=request_payload,
                model=actual_model_name,
                user_extra_data=_edit_extra,
            )
        except ChatBusyError as exc:
            raise chat_admission.chat_busy_http_exception(exc) from exc

        try:
            session_messages = chat_session_context._load_session_messages(
                chat_service, chat_id, db_user_id
            )
            kwargs = dict(
                accepted_run=accepted.run,
                chat_id=chat_id,
                user_id=db_user_id,
                session_messages=session_messages,
                effective_user_message=chat_context.build_effective_user_message(
                    init_message or execution_message,
                    edit_request.quoted_follow_up,
                    render_reference_block(saved_reference_cards),
                ),
                raw_user_message=body.new_content,
                context=context,
                model_name=actual_model_name,
            )
            if hasattr(accepted.run, "_sa_instance_state"):
                db.refresh(accepted.run)
                db.expunge(accepted.run)
            return kwargs
        except Exception as exc:
            ChatSequencer(db).abandon_pending_run(accepted.run.run_id, reason=str(exc))
            raise

    return await _start_prepared_run(prepare, db, chat_id)


async def _start_prepared_run(prepare, db: Session, chat_id: str):
    """Release admission locks before streaming; abandon admission if startup fails."""
    from core.services.chat_sequencer import ChatSequencer
    from orchestration import chat_run_executor

    def prepare_and_release():
        try:
            return prepare()
        finally:
            chat_session_context._release_request_session(db)

    kwargs = await run_in_threadpool(prepare_and_release)
    try:
        run = await chat_run_executor.start_run(**kwargs)
    except Exception as exc:
        reason = str(exc)

        def abandon():
            try:
                ChatSequencer(db).abandon_pending_run(kwargs["accepted_run"].run_id, reason=reason)
            finally:
                chat_session_context._release_request_session(db)

        await run_in_threadpool(abandon)
        raise
    return responses.sse_response(chat_run_executor.follow_run_as_sse(run.run_id, chat_id=chat_id))
