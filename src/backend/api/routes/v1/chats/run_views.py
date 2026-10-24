"""Active-run discovery and authorized durable SSE replay endpoints."""

from typing import Annotated

import api.transports.run_sse as run_sse
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.infra.responses as responses
import core.runs.runtime as runtime
import core.chat.operations.session_context as chat_session_context
from core.auth.backend import UserContext
from core.db.reserved_chats import reserved_chat_owner
from core.infra.logging import quiet_access_log
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/active-runs", summary="列出当前用户所有进行中的会话")
@quiet_access_log
def list_active_chat_runs(
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
    include_status: bool = False,
    recent_seconds: Annotated[
        int, Query(ge=0, description="recent_chat_ids 列出这段时间内起过 run 的会话")
    ] = 0,
):
    """一次性返回当前用户还在跑的会话，供侧边栏点亮「运行中」。

    ``/{chat_id}/active-run`` 只回答"我正打开的这段在不在跑"，所以换设备登录时
    侧边栏对没点开过的会话一无所知。这里按用户一次查完：查询只命中 pending /
    running 这一小撮行，与历史 run 的数量无关。
    """
    from core.chat.operations.runs import list_active_chat_runs as operation

    return operation(user=user, db=db, include_status=include_status, recent_seconds=recent_seconds)


@router.get("/stream/{run_id}", summary="续播 run（用于刷新后重新订阅）")
def chat_stream_resume(
    run_id: str,
    from_offset: int = Query(0, alias="from", ge=0, description="从此 offset 之后继续推送"),
    user: UserContext = Depends(auth_backend.get_current_user),
):
    """续播某个进行中的 run：刷新或断线后从指定 offset 之后重新订阅 SSE 事件。

    校验 run 存在且归属当前用户（非属主 403、不存在 404），返回 text/event-stream。
    """

    # A scoped session, not the request's: the stream below outlives this handler.
    with db_engine.SessionLocal() as db:
        db_user_id = chat_context.resolve_db_user_id(
            db, chat_session_context.authenticated_user_id(user)
        )
    run = runtime.get_run(run_id)
    # A module-owned chat's run is replayed by its module, not as an ordinary chat.
    if run is None or reserved_chat_owner(run.chat_id):
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != db_user_id:
        raise HTTPException(status_code=403, detail="无权访问该 run")
    resumed_chat_id = run.chat_id
    return responses.sse_response(
        run_sse.follow_run_as_sse(
            run_id, chat_id=resumed_chat_id, from_offset=from_offset
        ),
    )


@router.get("/{chat_id}/active-run", summary="探测会话是否有进行中的 run")
@quiet_access_log
def chat_active_run(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """探测会话当前是否有进行中的 run，供前端重连时决定是否续播。

    有则返回 run_id / message_id / status / 刷新重放 offset / thinking 模式等元信息，
    无则返回 data=null。需认证（有效 cookie 或 API-Key）。
    """
    from core.chat.operations.runs import chat_active_run as operation

    return operation(chat_id=chat_id, user=user, db=db)


@router.get("/stream/{run_id}/subscription", summary="订阅当前会话状态和后续事件")
def chat_run_subscription(
    run_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
):
    """Native ordinary-chat snapshot + tail; the raw replay API is separate."""
    from core.chat.projection.subscription import subscribe_run

    # A scoped session, not the request's: the stream below outlives this handler.
    with db_engine.SessionLocal() as db:
        db_user_id = chat_context.resolve_db_user_id(
            db, chat_session_context.authenticated_user_id(user)
        )
    run = runtime.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != db_user_id:
        raise HTTPException(status_code=403, detail="无权访问该 run")
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    if payload.get("kind", "chat") != "chat":
        raise HTTPException(status_code=400, detail="This run uses a specialized progress protocol")
    return responses.sse_response(subscribe_run(run_id))
