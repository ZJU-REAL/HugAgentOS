"""Revalidate project credentials without overriding project runtime capabilities."""

import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException


async def bind_project_api_runtime(request) -> None:
    context = request.project_ctx or {}
    scope = context.get("project_api_scope")
    if not scope:
        return
    if (
        not isinstance(scope, dict)
        or scope.get("version") != 1
        or not isinstance(scope.get("api_key_id"), str)
        or not scope.get("api_key_id")
        or scope.get("owner_user_id") != request.current_user_id
        or scope.get("project_id") != context.get("project_id")
    ):
        raise HTTPException(403, detail={"code": "invalid_project_api_scope"})

    def authorize():
        from core.db.engine import SessionLocal
        from core.auth.api_key_runs import require_admission_key

        with SessionLocal() as db:
            require_admission_key(
                db,
                {
                    "api_key_id": scope.get("api_key_id"),
                    "project_id": scope.get("project_id"),
                    "project_api_scope": scope,
                },
                request.current_user_id,
                datetime.now(timezone.utc),
            )

    await asyncio.to_thread(authorize)
