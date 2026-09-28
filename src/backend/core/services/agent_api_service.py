"""Scoped agent API admission and metadata-only call records."""

from __future__ import annotations

import hashlib
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError

from core.db.models import AgentApiCallLog, ChatRun, ChatSession, UserAgent, UserApiKey

# The mutable tracker is intentionally shared with the request's ASGI boundary
# across worker-thread contexts; it prevents duplicate rejection records.
call_log_tracking: ContextVar[dict | None] = ContextVar("agent_api_call_tracking", default=None)

_FORBIDDEN_INPUTS = (
    "attachments",
    "referenced_chats",
    "project_id",
    "mention_agent_id",
    "mention_name",
    "skill_id",
    "skill_name",
    "plugin_id",
    "plugin_name",
    "connector_id",
    "connector_name",
    "enabled_skills",
    "enabled_agents",
    "enabled_mcps",
    "enabled_kbs",
    "mode_slug",
    "plan_chat",
    "batch_chat",
    "workflow_chat",
    "site_chat",
    "quoted_follow_up",
    "model_provider_id",
)


def _now():
    return datetime.now(timezone.utc)


def require_agent_manager(db, user_id: str, agent_id: str):
    """Only the personal owner can manage an agent credential."""
    from core.services.user_agent_service import UserAgentService

    row = db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found"})
    try:
        UserAgentService(db)._check_ownership(row, user_id, "user")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "agent_management_required"}) from exc
    return row


def make_agent_api_scope(user, chat_id: str) -> dict:
    key_id = str(user.api_key_id)
    return {
        "version": 1,
        "owner_user_id": str(user.user_id),
        "api_key_id": key_id,
        "agent_id": str(user.api_key_agent_id),
        "chat_id": chat_id,
        "sandbox_user_id": f"api_{key_id}",
        "sandbox_session_id": "api_"
        + hashlib.sha256(f"{key_id}:{chat_id}".encode()).hexdigest()[:40],
    }


def session_matches_scope(session, scope: dict) -> bool:
    metadata = session.extra_data if isinstance(session.extra_data, dict) else {}
    return (
        session.user_id == scope["owner_user_id"]
        and session.deleted_at is None
        and session.project_id is None
        and session.channel_id is None
        and metadata.get("agent_id") == scope["agent_id"]
        and metadata.get("agent_api_scope") == scope
    )


def _bind_api_session(db, scope: dict, name: str):
    session = db.get(ChatSession, scope["chat_id"])
    if session is None:
        # The primary key arbitrates concurrent first calls. The losing insert
        # rereads and validates provenance rather than adopting an owner session.
        try:
            with db.begin_nested():
                session = ChatSession(
                    chat_id=scope["chat_id"],
                    user_id=scope["owner_user_id"],
                    title=f"{name} · API"[:500],
                    extra_data={
                        "agent_id": scope["agent_id"],
                        "agent_name": name,
                        "agent_api_scope": dict(scope),
                    },
                )
                db.add(session)
                db.flush()
            db.commit()
        except IntegrityError:
            db.expire_all()
            session = db.get(ChatSession, scope["chat_id"])
    if session is None or not session_matches_scope(session, scope):
        raise HTTPException(status_code=403, detail={"code": "api_session_scope_mismatch"})


def prepare_agent_api_request(db, user, request):
    """Bind only trusted authentication metadata; request bodies cannot set scope."""
    agent_id = getattr(user, "api_key_agent_id", None)
    if agent_id is None:
        session = db.get(ChatSession, request.chat_id)
        if session and (session.extra_data or {}).get("agent_api_scope"):
            raise HTTPException(status_code=403, detail={"code": "agent_api_key_required"})
        return request, None
    scope = make_agent_api_scope(user, request.chat_id)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request.chat_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_api_chat_id"})
        if not request.agent_id:
            raise HTTPException(status_code=422, detail={"code": "agent_id_required"})
        if request.agent_id != agent_id:
            raise HTTPException(status_code=403, detail={"code": "agent_key_target_mismatch"})
        for field in _FORBIDDEN_INPUTS:
            value = getattr(request, field, None)
            if value is not None and value is not False and value != [] and value != "":
                raise HTTPException(status_code=403, detail={"code": "api_capability_override"})
        if getattr(request, "chat_mode", None) == "turbo":
            raise HTTPException(status_code=403, detail={"code": "api_capability_override"})
        agent = require_agent_manager(db, str(user.user_id), str(agent_id))
        _bind_api_session(db, scope, agent.name)
        return request, scope
    except HTTPException as exc:
        call_id = begin_agent_api_call(db, scope, bool(getattr(request, "stream", False)))
        fail_agent_api_call(db, call_id, exc.status_code, exc.detail["code"])
        raise


def begin_agent_api_call(db, scope: dict, stream: bool, trace_id=None) -> str:
    key = db.get(UserApiKey, scope["api_key_id"])
    if key is None or key.user_id != scope["owner_user_id"] or key.agent_id != scope["agent_id"]:
        raise HTTPException(status_code=401, detail={"code": "invalid_api_key"})
    if not trace_id:
        from core.infra.logging import trace_id_var

        trace_id = trace_id_var.get()
    call_id = f"ac_{uuid.uuid4().hex}"
    db.add(
        AgentApiCallLog(
            id=call_id,
            user_id=scope["owner_user_id"],
            api_key_id=key.id,
            agent_id=key.agent_id,
            key_name=key.name,
            key_prefix=key.key_prefix,
            chat_id=scope["chat_id"] or None,
            stream=stream,
            trace_id=(trace_id or "")[:64] or None,
            status="running",
        )
    )
    db.commit()
    tracking = call_log_tracking.get()
    if tracking is not None:
        tracking["recorded"] = True
    return call_id


def bind_agent_api_run(db, call_id: str, run_id: str) -> None:
    row = db.get(AgentApiCallLog, call_id)
    run = db.get(ChatRun, run_id)
    scope = (run.request_payload or {}).get("agent_api_scope") if run else None
    if (
        not row
        or not scope
        or row.api_key_id != scope.get("api_key_id")
        or row.chat_id != run.chat_id
    ):
        raise HTTPException(status_code=403, detail={"code": "api_run_scope_mismatch"})
    row.run_id = run_id
    row.http_status = 200 if row.stream else None
    db.commit()


def fail_agent_api_call(db, call_id: str, status_code: int, error_code: str) -> None:
    # A failed admission transaction must not hide its bounded diagnostic fact.
    db.rollback()
    row = db.get(AgentApiCallLog, call_id)
    if row:
        row.status = "failed"
        row.http_status = status_code
        row.error_code = re.sub(r"[^A-Za-z0-9_-]", "_", str(error_code))[:64]
        row.completed_at = _now()
        db.commit()


def _utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def list_agent_api_calls(db, user_id, agent_id, *, page=1, page_size=20, key_id=None, status=None):
    # HTTP 200 means admitted for an SSE request; the run owns the execution
    # result even after disconnect or restart. Do not equate 200 with success.
    effective_status = case(
        (AgentApiCallLog.status == "failed", "failed"),
        (ChatRun.status.in_(("pending", "running")), "running"),
        (ChatRun.status.is_not(None), ChatRun.status),
        else_=AgentApiCallLog.status,
    )
    query = (
        db.query(AgentApiCallLog, ChatRun)
        .outerjoin(ChatRun, AgentApiCallLog.run_id == ChatRun.run_id)
        .filter(AgentApiCallLog.user_id == user_id, AgentApiCallLog.agent_id == agent_id)
    )
    if key_id:
        query = query.filter(AgentApiCallLog.api_key_id == key_id)
    if status:
        query = query.filter(effective_status == status)
    total = query.count()
    rows = (
        query.order_by(AgentApiCallLog.created_at.desc(), AgentApiCallLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for row, run in rows:
        outcome = row.status
        if outcome != "failed" and run is not None:
            outcome = "running" if run.status in ("pending", "running") else run.status
        completed = _utc(row.completed_at or (run.completed_at if run else None))
        created = _utc(row.created_at)
        usage = (run.usage or {}) if run else {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        items.append(
            {
                "id": row.id,
                "key_id": row.api_key_id,
                "key_name": row.key_name,
                "key_prefix": row.key_prefix,
                "agent_id": row.agent_id,
                "chat_id": row.chat_id,
                "run_id": row.run_id,
                "stream": row.stream,
                "status": outcome,
                "http_status": row.http_status,
                "error_code": row.error_code or ("run_failed" if outcome == "failed" else None),
                "created_at": created.isoformat(),
                "completed_at": completed.isoformat() if completed else None,
                "duration_ms": max(
                    0, int(((completed or _now()) - created).total_seconds() * 1000)
                ),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        )
    return items, total


def record_agent_api_http_status(run_id: str, status_code: int) -> None:
    """Record the completed JSON transport, independently of the run outcome."""
    import logging
    from sqlalchemy.exc import SQLAlchemyError
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            row = db.query(AgentApiCallLog).filter(AgentApiCallLog.run_id == run_id).first()
            if row is not None:
                row.http_status = status_code
                db.commit()
    except SQLAlchemyError:
        logging.getLogger(__name__).warning("agent_api_http_status_write_failed", exc_info=True)
