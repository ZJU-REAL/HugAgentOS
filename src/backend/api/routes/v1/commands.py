"""Authenticated background command status, output and cancellation."""
import asyncio

from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from core.sandbox.commands.runtime import runtime
from core.sandbox.commands import store
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

router = APIRouter(prefix="/v1/commands", tags=["Commands"])

@router.get("/events")
async def events(request: Request, chat_id: str):
    from core.sandbox.commands.subscription import authorize, subscribe
    user_id = await authorize(request, chat_id)
    return StreamingResponse(subscribe(request, chat_id, user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def authorized(task_id, user_id):
    try:
        row = store.snapshot(task_id)
    except LookupError as exc:
        raise HTTPException(404, "Command not found") from exc
    if row["user_id"] != user_id:
        raise HTTPException(404, "Command not found")
    return row

@router.get("")
def list_commands(chat_id: str, user: UserContext = Depends(get_current_user)):
    return success_response(store.list_owned(user.user_id, chat_id))

@router.get("/{task_id}/output")
def output(task_id: str, user: UserContext = Depends(get_current_user)):
    row = authorized(task_id, user.user_id)
    return success_response(store.read(task_id, user.user_id, row["sandbox_session_id"],
                                       consumer=None, consume_notice=False))

@router.post("/{task_id}/cancel")
async def cancel(task_id: str, user: UserContext = Depends(get_current_user)):
    row = await asyncio.to_thread(authorized, task_id, user.user_id)
    await runtime.cancel(task_id, user.user_id, row["sandbox_session_id"])
    latest = await asyncio.to_thread(store.snapshot, task_id)
    return success_response({"task_id": task_id, "status": latest["status"]})
