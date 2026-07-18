"""Plan mode API routes — generate and execute structured plans."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.infra.responses import success_response, sse_response
from core.services.plan_service import PlanService
from core.services.chat_service import ChatService
from core.services.user_model_selection import (
    UserModelSelectionError,
    resolve_effective_chat_model_name,
    resolve_user_model_provider_id,
)
from core.infra.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/plans", tags=["Plans"])


# ── Request Schemas ────────────────────────────────────────────


class HistoryMessage(BaseModel):
    role: str
    content: str


class PlanAttachment(BaseModel):
    name: str
    content: str = ""
    mime_type: str = ""
    file_id: str = ""
    download_url: str = ""


class GeneratePlanRequest(BaseModel):
    task_description: str = Field(..., min_length=1, max_length=5000)
    model_name: str = "qwen"
    model_provider_id: Optional[str] = Field(
        default=None,
        description="用户端模型切换选择的模型供应商 ID；仅在后台开关开启且供应商为 active chat 时生效",
        max_length=64,
    )
    enabled_mcp_ids: Optional[List[str]] = None
    enabled_skill_ids: Optional[List[str]] = None
    enabled_kb_ids: Optional[List[str]] = None
    enabled_agent_ids: Optional[List[str]] = None
    chat_id: Optional[str] = None
    history_messages: Optional[List[HistoryMessage]] = None
    attachments: Optional[List[PlanAttachment]] = None
    suppress_user_echo: bool = Field(
        default=False,
        description="主智能体经 enter_plan_mode 自动转入计划模式时置 True："
        "task_description 是 AI 扩写的内部提示词，**不落库为用户消息**，"
        "否则刷新后会作为用户气泡把内部提示词暴露到页面上。手动计划模式保持 "
        "False（用户亲手输入的任务应正常回显）。",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="若计划挂载于项目（Claude-style 工作空间），传项目 ID。会写入 "
        "chat_sessions.project_id，使会话出现在项目对话列表，并把项目 instructions/"
        "文件注入计划 agent。",
        max_length=64,
    )


class UpdatePlanRequest(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    steps: Optional[List[Dict[str, Any]]] = None


# ── SSE Helpers ────────────────────────────────────────────────


def _ensure_plan_session(
    db: Session,
    chat_id: Optional[str],
    user_id: str,
    project_id: Optional[str] = None,
) -> Optional[str]:
    """Ensure a chat session exists for plan mode messages.

    Returns the chat_id if session was created/found, None otherwise.

    If project_id is given and the session is not yet attached to any project, attach
    the session to that project — so the plan session appears in the project's chat
    list, and artifact archiving plus project-context injection work under the project
    scope. If it is already attached to another project it stays put (chats inside a
    project do not drift across projects), consistent with regular chats.
    """
    if not chat_id:
        return None
    try:
        svc = ChatService(db)
        svc.ensure_session(
            chat_id=chat_id,
            user_id=user_id,
            title="计划模式",
            extra_data={"plan_chat": True},
            project_id=project_id,
        )
        return chat_id
    except Exception as exc:
        logger.warning("Failed to ensure plan session: %s", exc)
        return None


def _save_plan_message(
    db: Session,
    chat_id: Optional[str],
    role: str,
    content: str,
    model: Optional[str] = None,
    extra_data: Optional[Dict] = None,
    tool_calls: Optional[List[Dict]] = None,
    usage: Optional[Dict] = None,
) -> None:
    """Save a message to the chat session for plan mode persistence."""
    if not chat_id or not content:
        return
    try:
        svc = ChatService(db)
        svc.add_message(
            chat_id=chat_id,
            role=role,
            content=content,
            model=model,
            extra_data=extra_data,
            tool_calls=tool_calls,
            usage=usage,
        )
    except Exception as exc:
        logger.warning("Failed to save plan message: %s", exc)


def _load_chat_history(
    db: Session,
    chat_id: Optional[str],
    user_id: str,
    history_messages: Optional[List[HistoryMessage]] = None,
) -> List[Dict[str, Any]]:
    """Load chat history as [{"role": ..., "content": ...}] dicts.

    Priority: DB lookup by chat_id > frontend-provided history_messages.
    Now that plan mode persists messages to DB, DB is the primary source.
    """
    # 1. Try DB first — same entry point as the main chat (checkpoint-aware +
    #    structured tool replay); long sessions get the compacted history rather than the full raw text.
    if chat_id:
        try:
            from core.services.compaction_service import load_session_history

            history = load_session_history(ChatService(db), chat_id, user_id)
            if history:
                return history
        except Exception as exc:
            logger.warning("Failed to load chat history from DB: %s", exc)

    # 2. Fallback: frontend-provided history
    if history_messages:
        return [{"role": m.role, "content": m.content} for m in history_messages if m.content]

    return []


# ── Endpoints ──────────────────────────────────────────────────


@router.post("/generate", summary="生成计划（SSE 流式）")
async def generate_plan(
    req: GeneratePlanRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SSE stream — generate a structured plan from a task description.

    后台 task 跑生成工作流，HTTP 连接断开（刷新页面）任务继续；前端可通过
    ``GET /v1/chats/{chat_id}/active-run`` 探测，结束后从消息列表拿到完整 plan。
    """
    db_chat_id = _ensure_plan_session(db, req.chat_id, user.user_id, project_id=req.project_id)
    if not db_chat_id:
        raise HTTPException(status_code=400, detail="chat_id 必填，无法创建后台生成任务")
    try:
        selected_model_provider_id = resolve_user_model_provider_id(
            db, req.model_provider_id, user_id=user.user_id
        )
    except UserModelSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    actual_model_name = resolve_effective_chat_model_name(
        selected_model_provider_id,
        fallback_model_name=req.model_name,
    )

    # On automatic entry into plan mode (enter_plan_mode), task_description is an
    # AI-expanded internal prompt and is NOT persisted as a user message — the original
    # user request and that main-agent turn are already in the same session history;
    # storing another copy would make a refresh display the internal prompt as a user
    # bubble. Manual plan mode (typed by the user) echoes normally.
    if not req.suppress_user_echo:
        _save_plan_message(
            db,
            db_chat_id,
            "user",
            req.task_description,
            model=actual_model_name,
            extra_data=(
                {"model_provider_id": selected_model_provider_id}
                if selected_model_provider_id
                else None
            ),
        )

    session_messages = _load_chat_history(db, req.chat_id, user.user_id, req.history_messages)
    logger.warning(
        "[plan-generate] chat_id=%s, loaded %d history messages", req.chat_id, len(session_messages)
    )

    uploaded_files = None
    if req.attachments:
        uploaded_files = [a.model_dump() for a in req.attachments if a.content]

    from orchestration import chat_run_executor

    run = await chat_run_executor.start_plan_generate_run(
        chat_id=db_chat_id,
        user_id=user.user_id,
        task_description=req.task_description,
        model_name=actual_model_name,
        model_provider_id=selected_model_provider_id,
        enabled_mcp_ids=req.enabled_mcp_ids,
        enabled_skill_ids=req.enabled_skill_ids,
        enabled_kb_ids=req.enabled_kb_ids,
        enabled_agent_ids=req.enabled_agent_ids,
        session_messages=session_messages,
        uploaded_files=uploaded_files,
    )

    return sse_response(
        chat_run_executor.follow_run_as_sse(
            run.run_id,
            chat_id=db_chat_id,
            error_event_factory=lambda reason: {"type": "plan_error", "error": reason},
        ),
    )


@router.get("", summary="计划列表")
async def list_plans(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """分页列出当前用户的全部计划。"""
    svc = PlanService(db)
    plans = svc.list_plans(user.user_id, limit=limit, offset=offset)
    return success_response(data=[PlanService.plan_to_dict(p) for p in plans])


@router.get("/{plan_id}", summary="计划详情")
async def get_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定计划的详情，含全部步骤；计划不存在返回 404。"""
    svc = PlanService(db)
    plan = svc.get_plan(plan_id, user.user_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")
    return success_response(data=PlanService.plan_to_dict(plan))


@router.patch("/{plan_id}", summary="更新计划")
async def update_plan(
    plan_id: str,
    req: UpdatePlanRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新计划：可改标题、审批（approved）/ 取消（cancelled）状态，或整体替换步骤列表。"""
    svc = PlanService(db)
    plan = svc.get_plan(plan_id, user.user_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")

    # Replace steps if provided
    if req.steps is not None:
        svc.replace_steps(plan_id, req.steps)

    # Update scalar fields
    updates = {}
    if req.status is not None:
        valid = {"approved", "cancelled"}
        if req.status not in valid:
            raise HTTPException(status_code=400, detail=f"只能设置状态为: {', '.join(valid)}")
        updates["status"] = req.status
    if req.title is not None:
        updates["title"] = req.title

    if updates:
        svc.update_plan(plan_id, **updates)

    plan = svc.get_plan(plan_id, user.user_id)
    return success_response(data=PlanService.plan_to_dict(plan))


@router.delete("/{plan_id}", summary="删除计划")
async def delete_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除指定计划及其全部步骤；计划不存在返回 404。"""
    svc = PlanService(db)
    deleted = svc.delete_plan(plan_id, user.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="计划不存在")
    return success_response(message="已删除")


class ExecutePlanRequest(BaseModel):
    enabled_mcp_ids: Optional[List[str]] = None
    enabled_skill_ids: Optional[List[str]] = None
    enabled_kb_ids: Optional[List[str]] = None
    enabled_agent_ids: Optional[List[str]] = None
    chat_id: Optional[str] = None
    history_messages: Optional[List[HistoryMessage]] = None
    project_id: Optional[str] = Field(
        default=None,
        description="若计划挂载于项目，传项目 ID（首次写入 chat_sessions.project_id）。",
        max_length=64,
    )


@router.post("/{plan_id}/execute", summary="执行计划（SSE 流式）")
async def execute_plan(
    plan_id: str,
    req: ExecutePlanRequest = ExecutePlanRequest(),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SSE stream — execute an approved plan step by step.

    后台 task 跑 ``astream_execute_plan``，事件流转到 Redis Stream；HTTP 连接
    断开（用户刷新页面）不会取消任务，前端可通过 ``GET /v1/chats/{chat_id}/active-run``
    + ``GET /v1/chats/stream/{run_id}`` 续播。
    """
    svc = PlanService(db)
    plan = svc.get_plan(plan_id, user.user_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")
    if plan.status != "approved":
        raise HTTPException(status_code=400, detail=f"计划状态为 '{plan.status}'，需要先审批")

    # Ensure chat session and save "确认执行" user message
    db_chat_id = _ensure_plan_session(db, req.chat_id, user.user_id, project_id=req.project_id)
    if not db_chat_id:
        raise HTTPException(status_code=400, detail="chat_id 必填，无法创建后台执行任务")
    actual_model_name = resolve_effective_chat_model_name() or "qwen"
    _save_plan_message(db, db_chat_id, "user", "确认执行", model=actual_model_name)

    session_messages = _load_chat_history(db, req.chat_id, user.user_id, req.history_messages)
    logger.warning(
        "[plan-execute] chat_id=%s, loaded %d history messages", req.chat_id, len(session_messages)
    )

    from orchestration import chat_run_executor

    run = await chat_run_executor.start_plan_execute_run(
        plan_id=plan_id,
        chat_id=db_chat_id,
        user_id=user.user_id,
        enabled_mcp_ids=req.enabled_mcp_ids,
        enabled_skill_ids=req.enabled_skill_ids,
        enabled_kb_ids=req.enabled_kb_ids,
        enabled_agent_ids=req.enabled_agent_ids,
        session_messages=session_messages,
        model_name=actual_model_name,
    )

    return sse_response(
        chat_run_executor.follow_run_as_sse(
            run.run_id,
            chat_id=db_chat_id,
            error_event_factory=lambda reason: {
                "type": "plan_error",
                "plan_id": plan_id,
                "error": reason,
            },
        ),
    )


@router.post("/{plan_id}/cancel", summary="取消计划")
async def cancel_plan(
    plan_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a running plan — 同时杀掉关联的后台 chat_run（如果有）。"""
    svc = PlanService(db)
    plan = svc.get_plan(plan_id, user.user_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在")
    if plan.status not in ("running", "approved", "draft"):
        raise HTTPException(status_code=400, detail=f"计划状态为 '{plan.status}'，无法取消")

    svc.update_plan(plan_id, status="cancelled")

    # Also cancel the background run in lockstep (only exists during the plan execute phase)
    from orchestration import chat_run_executor

    run = chat_run_executor.get_run_by_plan_id(plan_id)
    if run is not None:
        try:
            await chat_run_executor.cancel_run(run.run_id, user_id=user.user_id)
        except chat_run_executor.ChatRunPermissionDenied:
            pass  # the plan was already authorized; a run auth failure means user_id inference went wrong — do not block
        except Exception as exc:
            logger.warning(
                "plan_cancel_run_failed: plan_id=%s run_id=%s err=%s", plan_id, run.run_id, exc
            )

    return success_response(message="已取消")
