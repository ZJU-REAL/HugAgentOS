"""Shared HTTP rendering for durable main-chat admission."""

from fastapi import HTTPException

from core.services.chat_sequencer import ChatBusyError


def chat_busy_http_exception(exc: ChatBusyError) -> HTTPException:
    """Expose the active durable run without leaking database internals."""

    return HTTPException(
        status_code=409,
        detail={
            "code": "chat_busy",
            "message": "该会话已有任务正在运行，请续播当前任务或稍后再试",
            "active_run": {
                "run_id": exc.active_run.run_id,
                "message_id": exc.active_run.message_id,
                "status": exc.active_run.status,
            },
        },
    )
