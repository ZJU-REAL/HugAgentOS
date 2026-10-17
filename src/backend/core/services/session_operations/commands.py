"""Shared commands operations; callers supply authenticated identity."""

from core.infra.responses import success_response

from core.services.chat_steer_service import put_pending_steer

from core.services.steer_queue import SteerQueueConflict

from fastapi import HTTPException

from orchestration import chat_run_executor


async def cancel_chat_run(run_id, user=None):
    """取消正在执行的 chat run，真正杀掉后台任务；run 不存在返回 404，无权操作返回 403。"""
    try:
        cancelled = await chat_run_executor.cancel_run(run_id, user_id=user.user_id)
    except chat_run_executor.ChatRunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except chat_run_executor.ChatRunPermissionDenied:
        raise HTTPException(status_code=403, detail="无权取消该 run")
    return success_response(data={"run_id": run_id, "cancelled": cancelled})


async def steer_chat_run(run_id, body, user=None):
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
    try:
        accepted = await put_pending_steer(
            run_id,
            {
                "steer_id": body.steer_id,
                "message": message,
                "run_id": run_id,
                "chat_id": run.chat_id,
                "user_id": user.user_id,
                "delivery_mode": body.delivery_mode,
                "replace_latest": body.replace_latest,
            },
        )
    except SteerQueueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return success_response(data={**accepted, "queued": True})
