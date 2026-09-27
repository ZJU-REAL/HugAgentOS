"""ASGI boundary enforcing scoped Bearer credentials before any route auth.

Some management routes authenticate a session without get_current_user. The
middleware therefore runs for every route: a cookie or desktop bridge can never
widen a supplied agent key. The response body/SSE iterator is never buffered.
"""

import logging

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from core.auth.agent_api_scope import enforce_agent_api_route
from core.auth.backend import UserContext
from core.db.engine import SessionLocal
from core.infra.responses import error_response
from core.services.api_key_service import api_key_token_from_header, resolve_api_key_identity
from core.services.agent_api_service import (
    begin_agent_api_call,
    call_log_tracking,
    fail_agent_api_call,
    make_agent_api_scope,
)


class AgentApiScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        raw = api_key_token_from_header(request.headers.get("authorization", ""))
        if raw is None:
            return await self.app(scope, receive, send)

        def authorize():
            with SessionLocal() as db:
                identity = resolve_api_key_identity(db, raw)
                if identity is None:
                    raise HTTPException(status_code=401, detail="Invalid or expired API key")
                owner, key = identity
                context = UserContext(
                    user_id=owner.user_id,
                    user_center_id=owner.user_center_id or "",
                    username=owner.username,
                    api_key_id=key.id,
                    api_key_agent_id=key.agent_id,
                )
                enforce_agent_api_route(request, db, context)
                return context

        try:
            context = await run_in_threadpool(authorize)
        except HTTPException as exc:
            detail = exc.detail
            response = error_response(
                code=30002 if exc.status_code == 401 else 30004,
                message=(
                    detail.get("message", "API Key access denied")
                    if isinstance(detail, dict)
                    else str(detail)
                ),
                data={"reason": detail.get("code")} if isinstance(detail, dict) else {},
                status_code=exc.status_code,
            )
            return await response(scope, receive, send)
        except SQLAlchemyError:
            response = error_response(
                code=50000,
                message="API Key authentication temporarily unavailable",
                status_code=503,
            )
            return await response(scope, receive, send)
        path = request.url.path
        if path.startswith("/api/"):
            path = path[4:]
        if (
            context.api_key_agent_id is None
            or request.method != "POST"
            or path != "/v1/agents/responses"
        ):
            return await self.app(scope, receive, send)
        tracking = {"recorded": False}
        token = call_log_tracking.set(tracking)

        def record_validation_rejection(status):
            try:
                with SessionLocal() as db:
                    # No body is read or retained. A schema rejection has no
                    # accepted chat/run and its actual response transport is JSON.
                    call_id = begin_agent_api_call(db, make_agent_api_scope(context, ""), False)
                    fail_agent_api_call(
                        db,
                        call_id,
                        status,
                        "request_validation_failed" if status == 422 else "http_rejected",
                    )
            except SQLAlchemyError:
                logging.getLogger(__name__).warning("agent_api_rejection_log_failed", exc_info=True)

        async def scoped_send(message):
            if (
                message["type"] == "http.response.start"
                and message["status"] >= 400
                and not tracking["recorded"]
            ):
                await run_in_threadpool(record_validation_rejection, message["status"])
            await send(message)

        try:
            return await self.app(scope, receive, scoped_send)
        finally:
            call_log_tracking.reset(token)
