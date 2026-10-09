"""Active-run discovery and authorized durable SSE replay endpoints."""

import api.routes.v1.chats.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.infra.responses as responses
from core.auth.backend import UserContext
from core.infra.logging import quiet_access_log
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/active-runs", summary="列出当前用户所有进行中的会话")
@quiet_access_log
def list_active_chat_runs(
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """一次性返回当前用户还在跑的会话，供侧边栏点亮「运行中」。

    ``/{chat_id}/active-run`` 只回答"我正打开的这段在不在跑"，所以换设备登录时
    侧边栏对没点开过的会话一无所知。这里按用户一次查完：查询只命中 pending /
    running 这一小撮行，与历史 run 的数量无关。
    """
    from orchestration import chat_run_executor

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    runs = chat_run_executor.list_active_runs_for_user(db_user_id)
    return success_response(
        data={
            "items": [
                {
                    "chat_id": run.chat_id,
                    "run_id": run.run_id,
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                }
                for run in runs
            ]
        }
    )


@router.get("/stream/{run_id}", summary="续播 run（用于刷新后重新订阅）")
def chat_stream_resume(
    run_id: str,
    from_offset: int = Query(0, alias="from", ge=0, description="从此 offset 之后继续推送"),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """续播某个进行中的 run：刷新或断线后从指定 offset 之后重新订阅 SSE 事件。

    校验 run 存在且归属当前用户（非属主 403、不存在 404），返回 text/event-stream。
    """
    from orchestration import chat_run_executor

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    run = chat_run_executor.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != db_user_id:
        raise HTTPException(status_code=403, detail="无权访问该 run")
    resumed_chat_id = run.chat_id
    chat_session_context._release_request_session(db)
    return responses.sse_response(
        chat_run_executor.follow_run_as_sse(
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
    from orchestration import chat_run_executor

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    run = chat_run_executor.get_active_run_for_chat(chat_id, db_user_id)
    if run is None:
        return success_response(data=None)
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    kind = payload.get("kind", "chat")
    plan_id = payload.get("plan_id")
    # Resume needs the run's thinking mode: the SSE replay parser must start in
    # the right phase. The model emits reasoning with the opening <think> tag
    # frequently absent, so a wrong initial phase flattens it into the answer.
    resolved_mode = payload.get("chat_mode") or (
        "medium" if payload.get("enable_thinking", True) else "fast"
    )
    # This endpoint is a fresh-client probe: the server does not persist how
    # much of the stream this particular browser consumed.  ``run.last_event_offset``
    # is the producer high-water mark, not a consumer resume cursor.  Returning
    # it here would make a refreshed client skip the already-produced prefix
    # immediately after it clears the partial assistant message from the UI.
    replay_from_offset = 0
    return success_response(
        data={
            "run_id": run.run_id,
            "message_id": run.message_id,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "last_event_offset": replay_from_offset,
            "kind": kind,
            "plan_id": plan_id,
            "enable_thinking": resolved_mode not in ("fast", "turbo"),
        }
    )

@router.get("/stream/{run_id}/subscription", summary="订阅当前会话状态和后续事件")
def chat_run_subscription(
    run_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Native ordinary-chat snapshot + tail; the raw replay API is separate."""
    from orchestration import chat_run_executor
    from orchestration.run_subscription import subscribe_run

    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    run = chat_run_executor.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.user_id != db_user_id:
        raise HTTPException(status_code=403, detail="无权访问该 run")
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    if payload.get("kind", "chat") != "chat":
        raise HTTPException(status_code=400, detail="This run uses a specialized progress protocol")
    chat_session_context._release_request_session(db)
    return responses.sse_response(subscribe_run(run_id))
