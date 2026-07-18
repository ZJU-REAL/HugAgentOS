"""Autonomous Loop API — lifecycle of long-running autonomous loops (open in CE).

See internal design docs (§5).
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.infra.logging import get_logger
from core.infra.responses import success_response, sse_response
from core.services.chat_service import ChatService
from core.services.loop_service import LoopService
from orchestration import chat_run_executor

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/loops", tags=["AutonomousLoop"])


def _require_loop_cap(db: Session, user_id: str) -> None:
    from core.auth.capabilities import resolve_user_capabilities

    if not resolve_user_capabilities(db, user_id).get("can_run_autonomous_loop", False):
        raise HTTPException(status_code=403, detail="无自主循环权限（can_run_autonomous_loop）")


class GoalSpecIn(BaseModel):
    objective: str
    # Acceptance criteria (optional; if empty, the backend extracts them from
    # objective). The verdict is made by a read-only reviewer sub-agent that
    # personally verifies the real output — there are no more verify_cmd /
    # numeric-score / threshold script-verification fields (all removed, see
    # loop_reviewer).
    acceptance_criteria: List[str] = Field(default_factory=list)


class BudgetIn(BaseModel):
    max_iters: int = 50
    max_wall_clock_s: float = 6 * 3600.0
    max_tokens: int = 10_000_000


class CreateLoopReq(BaseModel):
    title: str = ""
    goal_spec: GoalSpecIn
    budget: BudgetIn = Field(default_factory=BudgetIn)
    # Conversation mode: bound to the originating session, so iterations feed
    # back into the current chat as ordinary conversation.
    chat_id: Optional[str] = None
    # The project the user selected in the input box — the loop is fully bound to
    # it; the worker operates directly in that project folder and publishing goes
    # through publish_site. If empty, it falls back to an isolated sandbox (a
    # purely task-oriented loop).
    project_id: Optional[str] = None


class StartLoopReq(BaseModel):
    model_name: Optional[str] = None
    evaluator_model: Optional[str] = None
    worker_max_iters: int = 15
    hitl_enabled: bool = False
    # Whether the worker enables thinking — legacy-client fallback bool
    # (fast=False, everything else=True).
    enable_thinking: bool = False
    # The user-confirmed thinking level (fast/medium/high/max) — passed through
    # verbatim to the worker's model resolution chain (_resolve_chat_mode →
    # reasoning_effort), ensuring "high/max" isn't flattened down to medium. When
    # provided, enable_thinking is derived from it; the bool is only a legacy-
    # client fallback.
    chat_mode: Optional[str] = None


def _loop_dict(loop) -> Dict[str, Any]:
    return {
        "loop_id": loop.loop_id,
        "title": loop.title,
        "status": loop.status,
        "goal_spec": loop.goal_spec,
        "budget": loop.budget,
        "iteration_count": loop.iteration_count,
        "tokens_spent": loop.tokens_spent,
        "final_score": float(loop.final_score) if loop.final_score is not None else None,
        "result_summary": loop.result_summary,
        "chat_id": loop.chat_id,
        "created_at": loop.created_at.isoformat() if loop.created_at else None,
    }


@router.post("", summary="创建自主循环")
def create_loop(
    req: CreateLoopReq,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    _require_loop_cap(db, user.user_id)
    loop = LoopService(db).create_loop(
        user_id=user.user_id,
        title=req.title,
        goal_spec=req.goal_spec.model_dump(),
        budget=req.budget.model_dump(),
        chat_id=req.chat_id,
        project_id=req.project_id,
    )
    return success_response(_loop_dict(loop))


@router.get("", summary="循环列表")
def list_loops(
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    loops = LoopService(db).list_loops(user.user_id)
    return success_response([_loop_dict(x) for x in loops])


@router.get("/{loop_id}", summary="循环详情")
def get_loop(
    loop_id: str,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    loop = LoopService(db).get_loop(loop_id, user_id=user.user_id)
    if not loop:
        raise HTTPException(status_code=404, detail="loop not found")
    return success_response(_loop_dict(loop))


@router.get("/{loop_id}/iterations", summary="循环审计轨迹")
def get_iterations(
    loop_id: str,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    svc = LoopService(db)
    if not svc.get_loop(loop_id, user_id=user.user_id):
        raise HTTPException(status_code=404, detail="loop not found")
    its = svc.list_iterations(loop_id)
    return success_response([
        {
            "seq": it.seq, "verdict": it.verdict,
            "score": float(it.score) if it.score is not None else None,
            "reasoning": it.reasoning, "tool_calls": it.tool_calls,
            "tokens": it.tokens, "decided_by": it.decided_by,
        }
        for it in its
    ])


async def _launch_loop(loop_id: str, req: StartLoopReq, db: Session, user: UserContext, is_resume: bool = False):
    _require_loop_cap(db, user.user_id)
    loop = LoopService(db).get_loop(loop_id, user_id=user.user_id)
    if not loop:
        raise HTTPException(status_code=404, detail="loop not found")

    chat_id = loop.chat_id or f"loopchat_{loop_id}"
    try:
        ChatService(db).ensure_session(
            chat_id=chat_id, user_id=user.user_id, title=f"[自主循环] {loop.title}",
            extra_data={"autonomous_loop": True, "loop_id": loop_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure loop session failed: %s", exc)
    if not loop.chat_id:
        loop.chat_id = chat_id
        db.commit()

    # chat_mode is the single source of truth for the thinking level; the
    # enable_thinking bool is only a legacy-client fallback.
    chat_mode = (req.chat_mode or "").strip().lower() or None
    if chat_mode not in (None, "fast", "medium", "high", "max"):
        chat_mode = None
    enable_thinking = (chat_mode != "fast") if chat_mode else req.enable_thinking
    # The project the loop is bound to (stored in metadata at creation) — the
    # worker/reviewer scope to the project folder based on it.
    project_id = (loop.extra_data or {}).get("project_id") if loop.extra_data else None
    run = await chat_run_executor.start_autonomous_loop_run(
        loop_id=loop_id,
        chat_id=chat_id,
        user_id=user.user_id,
        goal_spec=loop.goal_spec or {},
        budget=loop.budget or {},
        model_name=req.model_name,
        evaluator_model=req.evaluator_model,
        worker_max_iters=req.worker_max_iters,
        hitl_enabled=req.hitl_enabled,
        enable_thinking=enable_thinking,
        chat_mode=chat_mode,
        is_resume=is_resume,
        project_id=project_id,
    )
    return sse_response(
        chat_run_executor.follow_run_as_sse(
            run.run_id,
            chat_id=chat_id,
            error_event_factory=lambda reason: {
                "type": "loop_error", "loop_id": loop_id, "error": reason,
            },
        ),
    )


@router.post("/{loop_id}/start", summary="启动循环（SSE 流式）")
async def start_loop(
    loop_id: str,
    req: StartLoopReq,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    return await _launch_loop(loop_id, req, db, user)


@router.post("/{loop_id}/resume", summary="续跑循环（HITL 批准后 / 崩溃后，从 feature_list.json 断点续跑）")
async def resume_loop(
    loop_id: str,
    req: StartLoopReq,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    # Same entry point as start: when the driver detects a persisted
    # feature_list.json it automatically resumes from the checkpoint.
    # is_resume=True → don't re-record the objective as a user message again
    # (it was already recorded on first start).
    return await _launch_loop(loop_id, req, db, user, is_resume=True)


@router.post("/{loop_id}/cancel", summary="取消循环")
async def cancel_loop(
    loop_id: str,
    db: Session = Depends(get_db),
    user: UserContext = Depends(get_current_user),
):
    # Normalize chat_id consistently with _launch_loop: prefer loop.chat_id,
    # otherwise loopchat_{id}.
    loop = LoopService(db).get_loop(loop_id, user_id=user.user_id)
    chat_id = (loop.chat_id if loop else None) or f"loopchat_{loop_id}"
    run = chat_run_executor.get_active_run_for_chat(chat_id)
    if not run:
        raise HTTPException(status_code=404, detail="no active run for loop")
    ok = await chat_run_executor.cancel_run(run.run_id, user_id=user.user_id)
    return success_response({"cancelled": ok})
