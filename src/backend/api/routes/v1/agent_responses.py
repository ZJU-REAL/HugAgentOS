"""One durable admission path for streaming and complete agent responses."""

import asyncio

import api.transports.run_sse as run_sse
import core.chat.context as chat_context
import core.runs.runtime as runtime
import core.chat.service as chat_services_ChatService_module
from core.auth.backend import UserContext, get_current_user
from core.chat.schemas import ChatRequest, ChatResponse
from core.infra.logging import get_logger
from core.infra.responses import sse_response
from core.chat.operations.admission import start_response_run as _start_response_run
from core.db.threads import run_in_session
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["AgentResponses"])


async def _wait_response(run_id: str, *, chat_id: str, message_id: str) -> ChatResponse:
    terminal = await runtime.wait_run(run_id)
    if terminal.status == "cancelled":
        raise HTTPException(status_code=409, detail={"code": "run_cancelled", "run_id": run_id})
    if terminal.status == "needs_attention":
        raise HTTPException(
            status_code=409, detail={"code": "run_needs_attention", "run_id": run_id}
        )
    if terminal.status != "completed":
        raise HTTPException(
            status_code=500,
            detail=chat_context.resolve_user_facing_error(
                RuntimeError(terminal.error_message or "chat run failed")
            ),
        )
    return await run_in_session(_completed_response, chat_id, message_id)


def _completed_response(db: Session, chat_id: str, message_id: str) -> ChatResponse:
    assistant = chat_services_ChatService_module.ChatService(db).get_message_by_id(message_id)
    if assistant is None:
        raise HTTPException(status_code=500, detail="已完成的任务缺少回复")
    meta = assistant.extra_data or {}
    return ChatResponse(
        chat_id=chat_id,
        response=assistant.content,
        timestamp=chat_context.now_iso(),
        is_markdown=bool(meta.get("is_markdown", False)),
        route=meta.get("route", "main"),
        sources=meta.get("sources", []),
        artifacts=meta.get("artifacts", []),
        warnings=meta.get("warnings", []),
    )


@router.post(
    "/responses",
    summary="生成智能体回复（JSON / SSE）",
    response_model=ChatResponse,
    responses={
        200: {
            "description": "stream=false 返回 ChatResponse；stream=true 返回 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
)
async def agent_response(
    request: ChatRequest,
    user: UserContext = Depends(get_current_user),
):
    """同一请求通过 stream 选择完整 JSON 或 SSE；两者共享后台 Run 和权限校验。

    子智能体请求须显式传入 agent_id；专属 API Key 只能调用绑定的目标和 API 会话。SSE 断线可通过
    GET /v1/chats/stream/{run_id}?from=N 续播；HTTP 断开不会取消后台任务。
    """
    run = await _start_response_run(request, user)
    if request.stream:
        response = sse_response(run_sse.follow_run_as_sse(run.run_id, chat_id=run.chat_id))
        # This is the user row committed by admission, available before any
        # stream frames so clients can reconcile their optimistic message.
        response.headers["x-hugagent-user-message-id"] = run.user_message_id
        return response
    try:
        result = await _wait_response(run.run_id, chat_id=run.chat_id, message_id=run.message_id)
    except HTTPException as exc:
        await _record_http_status(user, run.run_id, exc.status_code)
        raise
    await _record_http_status(user, run.run_id, 200)
    return result


async def _record_http_status(user: UserContext, run_id: str, status_code: int) -> None:
    if user.api_key_agent_id:
        from core.auth.agent_api import record_agent_api_http_status

        await asyncio.to_thread(record_agent_api_http_status, run_id, status_code)
