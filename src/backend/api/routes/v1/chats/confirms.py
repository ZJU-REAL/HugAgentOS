"""Pending tool confirmations and user decision endpoints."""

import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.pending_access as chat_pending_access
import core.auth.backend as auth_backend
import core.db.engine as db_engine
from core.auth.backend import UserContext
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

router = APIRouter()


@router.get("/pending-confirms", summary="批量查询本人会话的待确认我的空间写操作")
async def list_pending_confirms(
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """前端刷新/首屏加载时一次性拉取——用于在侧边栏对应会话上点亮蓝点。

    注册表跨 worker 共享，逐个对候选 chat_id 做归属校验，只下发
    本人会话；待确认项数量天然很小（确认队列），逐条 DB 校验开销可忽略。

    注意：本路由必须声明在 ``GET /{chat_id}`` **之前**，否则会被 path
    参数路由吞掉（FastAPI 按声明顺序匹配）。
    """
    from core.llm.tools import _myspace_confirm as _mc

    items = []
    for cid in await _mc.list_pending_chat_ids_shared():
        if not await run_in_threadpool(
            chat_pending_access._owns_pending_chat, db, cid, user.user_id
        ):
            continue
        # Cannot just take "the latest one" (get_pending): when the latest is a
        # design_pick it would mask an earlier write-confirm in the same chat and
        # the blue dot would never light up. Prefer the latest write-confirm
        # pending; fall back to design_pick only if there is none (the frontend
        # renders that as merely lighting the blue dot).
        pendings = await _mc.get_all_pending_shared(cid)
        if not pendings:
            continue
        confirms = [p for p in pendings if p.get("kind") != _mc.KIND_DESIGN_PICK]
        rec = confirms[-1] if confirms else pendings[-1]
        items.append({"chat_id": cid, **rec})
    return success_response(data={"items": items})


@router.get("/{chat_id}/pending-confirm", summary="查询会话是否有待确认的我的空间写操作")
async def get_pending_confirm(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """前端刷新/切回该会话时恢复确认条（§13）。无待确认项时 pendings=[]。"""
    if not await run_in_threadpool(
        chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import _myspace_confirm as _mc

    # pendings: all outstanding items; the frontend restores the whole confirmation queue from this
    # (one round of parallel tool calls can concurrently register N distinct pending confirmations).
    return success_response(
        data={
            "pendings": await _mc.get_all_pending_shared(chat_id),
        }
    )


@router.post("/{chat_id}/file-confirm", summary="确认/拒绝对我的空间的写操作")
async def file_confirm(
    chat_id: str,
    body: chat_models.FileConfirmBody,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """用户带外批准/拒绝一次对「我的空间」的 Write/Edit/Delete/Move（§13）。

    模型**无法**自批——确认只能经此端点。批准后原工具调用恢复执行，
    不重试整个操作；拒绝则模型据工具反馈调整后续行为。
    """
    # Ownership check: must be the caller's own session
    if not await run_in_threadpool(
        chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import _myspace_confirm as _mc

    res = await _mc.set_decision_shared(
        chat_id, body.confirm_id, body.decision, option_id=body.option_id
    )
    if not res.get("ok"):
        # An expired confirmation (timeout reclaim / process restart) is not the user's fault —
        # return 200 with a stale flag so the frontend silently dismisses the zombie confirmation
        # bar and shows a friendly hint, instead of popping a 400.
        if res.get("reason") == "stale":
            # Plan F mid-term fix: distinguish "ordinary timeout" from "interruption caused by a
            # server restart". The latter needs a clearer message for the user (not "confirmation
            # timed out" but "session was interrupted, please resend"); the frontend shows a more
            # prominent notice for it instead of silently dismissing the bar.
            chat_interrupted = await run_in_threadpool(
                chat_pending_access._detect_chat_run_interrupted, db, chat_id
            )
            return success_response(
                data={
                    "ok": False,
                    "stale": True,
                    "chat_interrupted": chat_interrupted,
                    "message": (
                        "上次会话因服务端重启未完成，请重新发送您的消息"
                        if chat_interrupted
                        else res.get("error", "该确认已失效")
                    ),
                }
            )
        raise HTTPException(status_code=400, detail=res.get("error", "确认失败"))
    return success_response(data=res)
