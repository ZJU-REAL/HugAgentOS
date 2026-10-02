"""Pending user-question recovery, answer and cancellation endpoints."""

import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.pending_access as chat_pending_access
import core.auth.backend as auth_backend
import core.db.engine as db_engine
from core.auth.backend import UserContext
from core.infra.logging import quiet_access_log
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

router = APIRouter()


@router.get("/pending-user-questions", summary="批量查询本人会话的待回答问题")
@quiet_access_log
async def list_pending_user_questions(
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Restore sidebar waiting indicators without opening every conversation."""

    from core.llm.tools import user_questions

    items = []
    for chat_id in await user_questions.list_pending_chat_ids_shared():
        if not await run_in_threadpool(
            chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
        ):
            continue
        items.extend(
            {"chat_id": chat_id, **request}
            for request in await user_questions.get_all_pending_shared(chat_id)
        )
    return success_response(data={"items": items})


@router.get(
    "/{chat_id}/pending-user-questions",
    summary="查询会话中等待用户回答的问题",
)
async def get_pending_user_questions(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Return the authoritative pending queue for refresh/chat switching."""

    if not await run_in_threadpool(
        chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    return success_response(
        data={"requests": await user_questions.get_all_pending_shared(chat_id)},
    )


@router.post(
    "/{chat_id}/user-questions/{request_id}/answer",
    summary="回答智能体主动提出的问题",
)
async def answer_user_question(
    chat_id: str,
    request_id: str,
    body: chat_models.UserQuestionAnswerBody,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Validate the human answer and wake the exact suspended tool call."""

    if not await run_in_threadpool(
        chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    result = await user_questions.answer_shared(
        chat_id,
        request_id,
        [item.model_dump(exclude_none=True) for item in body.answers],
    )
    if result.get("ok"):
        return success_response(data=result)
    if result.get("reason") == "stale":
        interrupted = await run_in_threadpool(
            chat_pending_access._detect_chat_run_interrupted, db, chat_id
        )
        return success_response(
            data={
                **result,
                "stale": True,
                "chat_interrupted": interrupted,
                "message": (
                    "上次会话因服务端重启未完成，请重新发送您的消息"
                    if interrupted
                    else result.get("error", "该问题已失效")
                ),
            },
        )
    raise HTTPException(status_code=400, detail=result.get("error", "回答无效"))


@router.post(
    "/{chat_id}/user-questions/{request_id}/cancel",
    summary="取消智能体主动提出的问题",
)
async def cancel_user_question(
    chat_id: str,
    request_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Cancel the pending request; the tool receives an explicit non-retry result."""

    if not await run_in_threadpool(
        chat_pending_access._owns_pending_chat, db, chat_id, user.user_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    from core.llm.tools import user_questions

    result = await user_questions.cancel_shared(chat_id, request_id)
    if result.get("ok"):
        return success_response(data=result)
    interrupted = await run_in_threadpool(
        chat_pending_access._detect_chat_run_interrupted, db, chat_id
    )
    return success_response(
        data={
            **result,
            "stale": True,
            "chat_interrupted": interrupted,
            "message": (
                "上次会话因服务端重启未完成，请重新发送您的消息"
                if interrupted
                else result.get("error", "该问题已失效")
            ),
        },
    )
