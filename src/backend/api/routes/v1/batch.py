"""Public REST + SSE endpoints for the batch execution feature.

Endpoints:
  GET    /v1/batch/{plan_id}                  inspect a plan + its current progress
  POST   /v1/batch/{plan_id}/confirm          user-confirmed template; mark confirmed
  GET    /v1/batch/{plan_id}/stream           SSE: run BatchOrchestrator, stream events
  POST   /v1/batch/{plan_id}/cancel           flag plan as cancelled (orchestrator polls)
  POST   /v1/batch/{plan_id}/cancel-and-resume  SSE: cancel plan + delete the assistant
                                                turn that triggered it, then re-stream
                                                the user message with batch_plan disabled
                                                so the agent answers normally.

Phase 1 (LLM-driven) lives in the ``batch_runner`` MCP server which calls the
internal resolver in ``internal_batch.py``. Phase 2 (deterministic execution)
is triggered by the ``/stream`` endpoint here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.schemas import ChatRequest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import BatchPlan, ChatMessage
from core.infra.responses import success_response, sse_response
from orchestration.batch_orchestrator import BatchOrchestrator, cancel_running_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/batch", tags=["batch"])

# Mirrors the CHECK constraint in alembic v2w3x4y5z6a7_add_batch_plans.
PENDING = "pending"
CONFIRMED = "confirmed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL_STATUSES = {DONE, CANCELLED, FAILED}
_PRE_RUN_STATUSES = {PENDING, CONFIRMED}
_REPLAYABLE_STATUSES = {CONFIRMED, RUNNING, DONE, CANCELLED, FAILED}
_LIST_ACTIVE_STATUSES = (CONFIRMED, RUNNING, DONE, FAILED)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConfirmBody(BaseModel):
    prompt_template: str = Field(..., min_length=1, max_length=5000)
    max_retries: Optional[int] = Field(None, ge=0, le=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_owner(plan: BatchPlan, user: UserContext) -> None:
    if plan.user_id != str(user.user_id):
        raise HTTPException(status_code=403, detail="无权操作此批量计划")


def _plan_to_dict(plan: BatchPlan, *, include_results: bool = False) -> dict:
    progress = plan.progress or {"done": 0, "success": 0, "failed": 0}
    out: dict = {
        "plan_id": plan.plan_id,
        "chat_id": plan.chat_id,
        "source_type": plan.source_type,
        "instruction": plan.instruction,
        "items_total": len(plan.items or []),
        "items_preview": (plan.items or [])[:5],
        "placeholder_keys": plan.placeholder_keys or [],
        "prompt_template": plan.prompt_template,
        "max_retries": plan.max_retries,
        "status": plan.status,
        # ``progress`` may carry a nested ``results`` array; for the basic
        # listing call we strip it (the dedicated include_results flag
        # surfaces it) so we don't ship 100s of KB of per-item content
        # by default.
        "progress": {
            "done": int(progress.get("done", 0)),
            "success": int(progress.get("success", 0)),
            "failed": int(progress.get("failed", 0)),
        },
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
    }
    if include_results:
        out["item_results"] = list(progress.get("results") or [])
    return out


# ---------------------------------------------------------------------------
# GET /v1/batch/active?chat_id=...
# ---------------------------------------------------------------------------


@router.get("/active", summary="查询会话的活跃批量计划")
async def list_active_for_chat(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回指定会话下当前用户名下、未过期的批量计划（含进行中与已完成）。

    既包含进行中的计划（便于重新挂接 SSE 流继续观察），也包含已完成的计划
    （刷新后仍能看到结果）；已取消的计划被过滤。计划 24 小时 TTL（expires_at）
    决定其在会话中可见的时长，最多返回 50 条。
    """
    plans = (
        db.query(BatchPlan)
        .filter(BatchPlan.chat_id == chat_id)
        .filter(BatchPlan.user_id == str(user.user_id))
        # Skip cancelled plans — those represent user-rejected work and
        # would clutter the UI on every refresh. Done plans stay so the
        # final batch results are visible after refresh.
        .filter(BatchPlan.status.in_(_LIST_ACTIVE_STATUSES))
        .order_by(BatchPlan.created_at.desc())
        # Cap response size — old chats with many finished batches
        # shouldn't return megabytes on every page load.
        .limit(50)
        .all()
    )
    return success_response(data={"plans": [_plan_to_dict(p) for p in plans]})


# ---------------------------------------------------------------------------
# GET /v1/batch/{plan_id}
# ---------------------------------------------------------------------------


@router.get("/{plan_id}", summary="查询批量计划详情")
async def get_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按 plan_id 查询批量计划详情（含逐项结果），仅限计划所有者访问。"""
    plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已过期")
    _ensure_owner(plan, user)
    # Include per-item results so the frontend can hydrate finished plans
    # on chat-load without opening an SSE stream.
    return success_response(data=_plan_to_dict(plan, include_results=True))


# ---------------------------------------------------------------------------
# POST /v1/batch/{plan_id}/confirm
# ---------------------------------------------------------------------------


@router.post("/{plan_id}/confirm", summary="确认批量计划")
async def confirm_plan(
    plan_id: str,
    body: ConfirmBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认批量计划：写入用户编辑后的 prompt 模板与重试次数，状态置为 confirmed。

    仅限计划所有者，且计划须处于 pending/confirmed 状态，确认后方可执行。
    """
    plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已过期")
    _ensure_owner(plan, user)
    if plan.status not in _PRE_RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"计划状态 {plan.status} 不允许修改并确认",
        )

    plan.prompt_template = body.prompt_template.strip()
    if body.max_retries is not None:
        plan.max_retries = body.max_retries
    plan.status = CONFIRMED
    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)
    return success_response(data=_plan_to_dict(plan))


# ---------------------------------------------------------------------------
# POST /v1/batch/{plan_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/{plan_id}/cancel", summary="取消批量计划")
async def cancel_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消批量计划：状态置为 cancelled 并中断进行中的执行任务（仅限所有者）。"""
    plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已过期")
    _ensure_owner(plan, user)
    plan.status = CANCELLED
    plan.updated_at = datetime.utcnow()
    db.commit()
    # Interrupt the in-flight runner task too. Without this the runner
    # only checks plan.status between items, so a long-running LLM call
    # would have to finish before cancellation took effect.
    cancelled_task = cancel_running_task(plan_id)
    return success_response(
        data={"plan_id": plan_id, "status": CANCELLED, "task_cancelled": cancelled_task}
    )


# ---------------------------------------------------------------------------
# POST /v1/batch/{plan_id}/cancel-and-resume
# ---------------------------------------------------------------------------


def _assistant_msg_has_batch_plan_call(msg: ChatMessage, plan_id: str) -> bool:
    """Detect whether *msg* is the assistant turn that fired ``batch_plan``."""
    if msg.role != "assistant":
        return False
    tcs = msg.tool_calls or []
    if not isinstance(tcs, list):
        return False
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        if tc.get("tool_name") == "batch_plan" or tc.get("name") == "batch_plan":
            # Prefer to match the specific plan if the result was already
            # attached to this message (post-stream persistence path).
            output = tc.get("output") or tc.get("result") or {}
            if isinstance(output, dict):
                inner = output.get("result", output)
                if isinstance(inner, dict) and inner.get("plan_id") == plan_id:
                    return True
            # Fallback: accept any batch_plan call (assistant turn that
            # never received the result has empty output).
            return True
    return False


@router.post("/{plan_id}/cancel-and-resume", summary="取消批量并以普通工具继续回答 (SSE)")
async def cancel_and_resume(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消批量计划，删除触发 batch_plan 的悬空助手轮，并带 disable_batch_plan
    标志重新流式回放原始用户消息，使模型改走普通工具或直接作答（SSE 输出）。
    """
    # Lazy imports — these helpers live in chats.py and pull in heavy
    # session/message infrastructure, so we keep them out of module init.
    from api.routes.v1.chats import (
        _build_ctx,
        _build_effective_user_message,
        _ensure_main_model_configured,
        _load_session_messages,
        _restore_attachments,
        _authenticated_user_id,
    )
    from core.services import ChatService, UserService
    from core.chat.context import resolve_enabled_capabilities, resolve_db_user_id
    from orchestration import chat_run_executor

    _ensure_main_model_configured()

    plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已过期")
    _ensure_owner(plan, user)

    if not plan.chat_id:
        raise HTTPException(
            status_code=400,
            detail="该计划未关联会话，无法回退到普通对话流程",
        )

    # Mark cancelled (idempotent; orchestrator polls this on each item).
    if plan.status not in _TERMINAL_STATUSES:
        plan.status = CANCELLED
        plan.updated_at = datetime.utcnow()
        db.commit()

    chat_id = plan.chat_id
    db_user_id = resolve_db_user_id(db, _authenticated_user_id(user))
    chat_service = ChatService(db)

    # ── Find the assistant turn that triggered this plan ──
    page_size = 50
    listing = chat_service.list_messages(chat_id, db_user_id, page=1, page_size=page_size)
    if not listing:
        raise HTTPException(status_code=404, detail="找不到关联会话")
    messages, _total, _pages = listing

    target_assistant: Optional[ChatMessage] = None
    for msg in reversed(messages):
        if _assistant_msg_has_batch_plan_call(msg, plan_id):
            target_assistant = msg
            break

    if not target_assistant:
        raise HTTPException(
            status_code=400,
            detail="未找到触发本批量计划的助手消息，无法继续",
        )

    user_msg = chat_service.get_user_message_before(chat_id, target_assistant.message_id)
    if not user_msg:
        raise HTTPException(
            status_code=400,
            detail="未找到对应的用户消息，无法继续",
        )

    user_content = user_msg.content
    user_extra = user_msg.extra_data or {}
    attachment_items = _restore_attachments(user_extra.get("attachments", []))

    # Drop the empty/dangling assistant turn so chat history stays clean.
    chat_service.delete_messages_from(chat_id, target_assistant.message_id)

    # ── Build a regular ChatRequest that disables the batch tool ──
    # Restore the original thinking-mode preference from the user message
    # so that "fast mode" runs stay in fast mode after cancel-and-resume.
    resume_request = ChatRequest(
        chat_id=chat_id,
        message=user_content,
        model_name=target_assistant.model or "qwen",
        enable_thinking=user_extra.get("enable_thinking", True),
        quoted_follow_up=user_extra.get("quoted_follow_up"),
        attachments=attachment_items,
        disable_batch_plan=True,
    )

    enabled_skills, enabled_agents, enabled_mcps = resolve_enabled_capabilities(
        db, db_user_id
    )
    _user_settings = UserService(db).get_user_settings(db_user_id)
    effective_msg = _build_effective_user_message(
        resume_request.message, resume_request.quoted_follow_up
    )

    session_messages = _load_session_messages(chat_service, chat_id, db_user_id)
    session_messages.append({"role": "user", "content": effective_msg})
    context = _build_ctx(
        resume_request,
        db_user_id,
        enabled_skills,
        enabled_agents,
        enabled_mcps,
        memory_enabled=bool(_user_settings.get("memory_enabled", False)),
        memory_write_enabled=bool(_user_settings.get("memory_write_enabled", False)),
        reranker_enabled=bool(_user_settings.get("reranker_enabled", False)),
        ontology_enabled=bool(_user_settings.get("ontology_enabled", False)),
        ontology_pack_ids=_user_settings.get("ontology_pack_ids") or None,
    )

    # Use the modern chat_run_executor pipeline so:
    #   • the run is registered in `chat_runs` (visible as "in progress")
    #   • the orchestration task survives client disconnect (refresh resumes)
    #   • thinking events flow through the same Redis-backed stream that
    #     processRegenerateStream knows how to render
    run = await chat_run_executor.start_run(
        chat_id=chat_id,
        user_id=db_user_id,
        session_messages=session_messages,
        effective_user_message=effective_msg,
        raw_user_message=resume_request.message,
        context=context,
        request_payload=resume_request.model_dump(exclude_none=True),
        model_name=resume_request.model_name,
    )

    return sse_response(
        chat_run_executor.follow_run_as_sse(run.run_id, chat_id=chat_id),
    )


# ---------------------------------------------------------------------------
# GET /v1/batch/{plan_id}/stream  (SSE — runs the orchestrator)
# ---------------------------------------------------------------------------


@router.get("/{plan_id}/stream", summary="执行批量计划 (SSE)")
async def stream_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """以 SSE 运行批量编排器，逐项流式推送执行进度与结果（仅限所有者）。

    计划须处于可回放状态（confirmed/running/done/cancelled/failed）；对已完成
    计划重连时会回放已存储的逐项结果，便于刷新或切换会话后恢复展示。
    """
    plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已过期")
    _ensure_owner(plan, user)
    # Accept reconnection after completion as well: the orchestrator replays
    # already-stored item results so the user sees full history when they
    # come back to the chat (page refresh / chat switch).
    if plan.status not in _REPLAYABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"计划必须先确认才能执行（当前状态={plan.status}）",
        )

    user_id = str(user.user_id)

    async def _gen():
        orchestrator = BatchOrchestrator(plan_id, user_id)
        try:
            async for event in orchestrator.run():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Client disconnected; orchestrator state is preserved in DB.
            logger.info("[batch.stream] client disconnected plan_id=%s", plan_id)
            raise
        except Exception as exc:
            logger.exception("[batch.stream] orchestrator failed plan_id=%s", plan_id)
            err_payload = {
                "type": "batch_error",
                "plan_id": plan_id,
                "error": str(exc)[:500],
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return sse_response(_gen())
