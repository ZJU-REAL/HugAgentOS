"""Chat Run management API — cancel and durable queued input."""

import core.runs.runtime as runtime
from core.auth.backend import UserContext, get_current_user
from core.infra.logging import get_logger
from core.infra.responses import success_response
from core.chat.steer import list_run_steers, remove_pending_steer
from core.chat.operations.schemas import SteerChatRunRequest
from fastapi import APIRouter, Depends, HTTPException

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/chat-runs", tags=["ChatRuns"])


@router.post("/{run_id}/cancel", summary="取消正在执行的 run（真正杀掉后台任务）")
async def cancel_chat_run(
    run_id: str,
    user: UserContext = Depends(get_current_user),
):
    """取消正在执行的 chat run，真正杀掉后台任务；run 不存在返回 404，无权操作返回 403。"""
    from core.chat.operations.commands import cancel_chat_run as operation

    return await operation(run_id=run_id, user=user)


@router.post("/{run_id}/steer", summary="耐久排队 steer、followUp 或 nextRun")
async def steer_chat_run(
    run_id: str,
    body: SteerChatRunRequest,
    user: UserContext = Depends(get_current_user),
):
    """Queue one instruction for the live ReAct loop's next safe boundary."""
    from core.chat.operations.commands import steer_chat_run as operation

    return await operation(run_id=run_id, body=body, user=user)


@router.get("/{run_id}/steers", summary="查询该 Run 的耐久排队状态")
async def list_chat_run_steers(
    run_id: str,
    user: UserContext = Depends(get_current_user),
):
    run = runtime.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该 run")
    return success_response(data={"run_id": run_id, "items": list_run_steers(run_id)})


@router.delete("/{run_id}/steer/{steer_id}", summary="撤回尚未生效的追加指令")
async def withdraw_chat_run_steer(
    run_id: str,
    steer_id: str,
    user: UserContext = Depends(get_current_user),
):
    run = runtime.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该 run")
    removed = await remove_pending_steer(run_id, steer_id)
    return success_response(data={"run_id": run_id, "steer_id": steer_id, "removed": removed})


@router.get("/{run_id}", summary="获取后台运行状态")
async def get_chat_run(run_id: str, user: UserContext = Depends(get_current_user)):
    run = runtime.get_run(run_id)
    if run is None or run.user_id != user.user_id:
        raise HTTPException(404, "Run not found")
    return success_response(
        data={
            "run_id": run.run_id,
            "chat_id": run.chat_id,
            "message_id": run.message_id,
            "status": run.status,
        }
    )
