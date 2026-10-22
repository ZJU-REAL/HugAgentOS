"""Shared runs operations; callers supply authenticated identity."""

import core.chat.context as chat_context
import core.runs.runtime as runtime
from core.auth.backend import UserContext
from core.infra.responses import success_response


def list_active_chat_runs(user=None, db=None, include_status=False, recent_seconds=0):
    """一次性返回当前用户还在跑的会话，供侧边栏点亮「运行中」。

    ``/{chat_id}/active-run`` 只回答"我正打开的这段在不在跑"，所以换设备登录时
    侧边栏对没点开过的会话一无所知。这里按用户一次查完：查询只命中 pending /
    running 这一小撮行，与历史 run 的数量无关。
    """
    pass

    db_user_id = chat_context.resolve_db_user_id(
        db, user.user_id if isinstance(user, UserContext) else None
    )
    runs, recent_chat_ids = runtime.list_active_runs_for_user(db_user_id, recent_seconds)
    from core.chat.sidebar_status import sidebar_run_statuses

    return success_response(
        data={
            **({"statuses": sidebar_run_statuses(db, db_user_id)} if include_status else {}),
            "items": [
                {
                    "chat_id": run.chat_id,
                    "run_id": run.run_id,
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                }
                for run in runs
            ],
            "recent_chat_ids": recent_chat_ids,
        }
    )


def chat_active_run(chat_id, user=None, db=None):
    """探测会话当前是否有进行中的 run，供前端重连时决定是否续播。

    有则返回 run_id / message_id / status / 刷新重放 offset / thinking 模式等元信息，
    无则返回 data=null。需认证（有效 cookie 或 API-Key）。
    """
    pass

    db_user_id = chat_context.resolve_db_user_id(
        db, user.user_id if isinstance(user, UserContext) else None
    )
    run = runtime.get_active_run_for_chat(chat_id, db_user_id)
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
