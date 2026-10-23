"""Bind durable admission and cancellation to the credential lifecycle."""

from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Mapping

from core.db.models import AgentApiCallLog, ChatRun, UserApiKey
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# Authentication sets this for the whole request, including worker threads.
request_api_key: ContextVar[tuple[str, str] | None] = ContextVar("request_api_key", default=None)


def bind_request_key(payload: Mapping[str, Any], user_id: str) -> dict[str, Any]:
    payload = dict(payload or {})
    identity = request_api_key.get()
    if identity is not None:
        owner, key_id = identity
        if owner != user_id:
            raise HTTPException(status_code=401, detail={"code": "invalid_api_key"})
        payload["api_key_id"] = key_id
    return payload


def require_admission_key(
    db: Session, payload: Mapping[str, Any], user_id: str, now: datetime
) -> None:
    """Lock the credential until acceptance commits, serializing revoke/admit."""
    scope = payload.get("agent_api_scope") or {}
    key_id = payload.get("api_key_id") or scope.get("api_key_id")
    if not key_id:
        return
    key = (
        db.query(UserApiKey)
        .filter(UserApiKey.id == key_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    expiry = key.expires_at if key else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (
        key is None
        or key.user_id != user_id
        or not key.enabled
        or key.revoked_at is not None
        or (expiry is not None and expiry <= now)
    ):
        raise HTTPException(status_code=401, detail={"code": "invalid_api_key"})
    project_id = payload.get("project_id")
    project_scope = payload.get("project_api_scope") or {}
    if key.project_id is not None:
        from core.projects.api import require_project_key_access

        require_project_key_access(db, user_id, key.project_id)
        if (
            project_id != key.project_id
            or project_scope.get("project_id") != key.project_id
            or project_scope.get("api_key_id") != key_id
            or project_scope.get("owner_user_id") != user_id
        ):
            raise HTTPException(status_code=403, detail={"code": "project_key_target_mismatch"})
    elif project_scope:
        raise HTTPException(status_code=403, detail={"code": "invalid_project_api_scope"})
    if scope and (
        scope.get("owner_user_id") != user_id
        or scope.get("api_key_id") != key_id
        or scope.get("agent_id") != key.agent_id
    ):
        raise HTTPException(status_code=401, detail={"code": "invalid_api_key"})


def cancel_key_runs(db: Session, key_id: str, user_id: str, now: datetime) -> None:
    """Cancel live runs in the same transaction that disables their credential.

    Payload bindings cover personal keys and accepted-but-not-yet-logged runs;
    historical agent call records also identify pre-binding scoped runs.
    """
    from core.harness.registry.run_cancellation import cancel_run_in_session

    rows = (
        _key_runs_query(db, key_id, user_id)
        .filter(ChatRun.status.in_(("pending", "running", "needs_attention")))
        .order_by(ChatRun.run_id)
        .populate_existing()
        .with_for_update()
        .all()
    )
    for row in rows:
        cancel_run_in_session(db, row, reason="API key disabled or revoked", now=now)


def _cancelled_key_runs(db: Session, key_id: str, user_id: str) -> list[str]:
    return [
        run_id
        for (run_id,) in _key_runs_query(db, key_id, user_id)
        .with_entities(ChatRun.run_id)
        .filter(
            ChatRun.status == "cancelled",
            ChatRun.failure_reason == "API key disabled or revoked",
        )
        .all()
    ]


async def stop_cancelled_key_workers(key_id: str, user_id: str) -> None:
    """Wake SSE followers and stop local workers after the durable commit."""
    from core.db.threads import run_in_session
    from core.harness.registry.run_cancellation import cancel_run

    for run_id in await run_in_session(_cancelled_key_runs, key_id, user_id):
        await cancel_run(run_id, user_id=user_id, stop_terminal_worker=True)


def authorize_run_execution(db: Session, run_id: str, now: datetime) -> bool:
    """Reject and durably fence work whose credential is already invalid."""
    from core.harness.registry.run_cancellation import cancel_run_in_session

    run = db.get(ChatRun, run_id)
    if run is None:
        return False
    try:
        require_admission_key(db, run.request_payload or {}, run.user_id, now)
        return True
    except HTTPException:
        run = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        if run.status in ("pending", "running", "needs_attention"):
            cancel_run_in_session(db, run, reason="API key disabled or revoked", now=now)
        db.commit()
        return False


def _key_runs_query(db: Session, key_id: str, user_id: str):
    logged = select(AgentApiCallLog.run_id).where(AgentApiCallLog.api_key_id == key_id)
    return db.query(ChatRun).filter(
        ChatRun.user_id == user_id,
        or_(
            ChatRun.request_payload["api_key_id"].as_string() == key_id,
            ChatRun.request_payload["agent_api_scope"]["api_key_id"].as_string() == key_id,
            ChatRun.run_id.in_(logged),
        ),
    )
