"""Chat Run management API — cancel and mid-run steering."""

from core.auth.backend import UserContext, get_current_user
from core.infra.logging import get_logger
from core.infra.responses import success_response
from core.services.chat_steer_service import put_pending_steer, remove_pending_steer
from fastapi import APIRouter, Depends, HTTPException
from orchestration import chat_run_executor
from pydantic import BaseModel, Field, field_validator

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/chat-runs", tags=["ChatRuns"])


class SteerChatRunRequest(BaseModel):
    steer_id: str = Field(..., min_length=1, max_length=64, description="前端待发送卡片 ID")
    message: str = Field(..., min_length=1, max_length=10000, description="追加给当前 run 的指令")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be empty")
        return value


@router.post("/{run_id}/cancel", summary="取消正在执行的 run（真正杀掉后台任务）")
async def cancel_chat_run(
    run_id: str,
    user: UserContext = Depends(get_current_user),
):
    """取消正在执行的 chat run，真正杀掉后台任务；run 不存在返回 404，无权操作返回 403。"""
    try:
        cancelled = await chat_run_executor.cancel_run(run_id, user_id=user.user_id)
    except chat_run_executor.ChatRunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except chat_run_executor.ChatRunPermissionDenied:
        raise HTTPException(status_code=403, detail="无权取消该 run")
    return success_response(data={"run_id": run_id, "cancelled": cancelled})


@router.post("/{run_id}/steer", summary="在下一次安全 ReAct 边界追加指令")
async def steer_chat_run(
    run_id: str,
    body: SteerChatRunRequest,
    user: UserContext = Depends(get_current_user),
):
    """Queue one instruction for the live ReAct loop's next safe boundary."""
    run = chat_run_executor.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该 run")
    if run.status not in ("pending", "running"):
        raise HTTPException(status_code=409, detail="run 已结束")
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    if payload.get("kind", "chat") != "chat":
        raise HTTPException(status_code=409, detail="当前运行模式不支持 Steer")

    message = body.message.strip()
    await put_pending_steer(
        run_id,
        {
            "steer_id": body.steer_id,
            "message": message,
            "run_id": run_id,
            "chat_id": run.chat_id,
        },
    )
    return success_response(data={"run_id": run_id, "steer_id": body.steer_id, "queued": True})


@router.delete("/{run_id}/steer/{steer_id}", summary="撤回尚未生效的追加指令")
async def withdraw_chat_run_steer(
    run_id: str,
    steer_id: str,
    user: UserContext = Depends(get_current_user),
):
    run = chat_run_executor.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该 run")
    removed = await remove_pending_steer(run_id, steer_id)
    return success_response(data={"run_id": run_id, "steer_id": steer_id, "removed": removed})
