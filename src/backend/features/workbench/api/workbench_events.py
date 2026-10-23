"""Bounded revision notifications; each check resolves current identity and projection."""

import asyncio
import time

from features.workbench.directory_revision import snapshot
from core.auth.backend import UserContext, get_current_user, require_auth
from core.db.engine import SessionLocal
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

WAIT_SECONDS = 25
CHECK_SECONDS = 2


def current_snapshot(user_id):
    with SessionLocal() as db:
        return snapshot(db, user_id)


async def directory_events(
    request: Request,
    response: Response,
    revision: str = Query(default="", max_length=64, pattern="^[a-f0-9]*$"),
    user: UserContext = Depends(require_auth()),
):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        # No request-long transaction or cached user capabilities. Recheck session and
        # projection even if Last-Event-ID/revision comes from an authenticated client.
        current = await get_current_user(request, None)
        if current.user_id != user.user_id:
            raise HTTPException(401, detail={"code": "session_revoked"})
        data, scope = await run_in_threadpool(current_snapshot, current.user_id)
        if data["revision"] != revision:
            response.headers["Cache-Control"] = "private, no-store"
            return success_response(data={"revision": data["revision"], "scope": scope})
        if await request.is_disconnected() or time.monotonic() >= deadline:
            response.status_code = 204
            response.headers["Cache-Control"] = "private, no-store"
            return None
        await asyncio.sleep(CHECK_SECONDS)


def routes(route_class):
    router = APIRouter(route_class=route_class)
    router.add_api_route("/catalog/events", directory_events, methods=["GET"])
    return router
