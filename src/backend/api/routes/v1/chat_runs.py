"""Chat Run management API — currently only exposes cancel; list/detail are not needed for now."""

from fastapi import APIRouter, Depends, HTTPException

from core.auth.backend import get_current_user, UserContext
from core.infra.logging import get_logger
from core.infra.responses import success_response
from orchestration import chat_run_executor

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/chat-runs", tags=["ChatRuns"])


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
