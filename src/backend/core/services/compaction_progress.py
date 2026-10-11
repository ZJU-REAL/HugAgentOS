"""Small durable UI projection; never included in model context or tool counts."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)
# Session observations are bounded; assistant rows retain older timeline steps.
_PROGRESS_RETENTION_LIMIT = 20
# Allow checkpoint persistence and event delivery after the summary request.
_PROGRESS_EXPIRY_GRACE_SECONDS = 120
_sink: ContextVar[Any] = ContextVar("compaction_progress_sink", default=None)
_identity: ContextVar[tuple[str, str]] = ContextVar(
    "compaction_progress_identity", default=("", "")
)
_context: ContextVar[Any] = ContextVar("compaction_progress_context", default=None)


@contextmanager
def progress_scope(sink, chat_id: str = "", run_id: str = "", *, context=None):
    context_token = _context.set(context)
    identity_token = _identity.set((chat_id, run_id))
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
        _identity.reset(identity_token)
        _context.reset(context_token)


def persist_progress(event: dict) -> None:
    if not event.get("run_id"):
        return
    from core.db.engine import SessionLocal
    from core.db.models import ChatMessage, ChatRun, ChatSession

    with SessionLocal() as db:
        session = (
            db.query(ChatSession).filter_by(chat_id=event["chat_id"]).with_for_update().first()
        )
        if session is None:
            return
        run = (
            db.query(ChatRun).filter_by(run_id=event["run_id"], chat_id=event["chat_id"]).first()
            if event["run_id"]
            else None
        )
        stored = {
            **event,
            "message_id": event.get("message_id") or getattr(run, "message_id", "") or "",
        }
        extra = dict(session.extra_data or {})
        events = list(extra.get("compaction_progress") or [])
        previous = next((item for item in events if item.get("id") == event["id"]), None)
        if event["status"] == "running" and previous and previous.get("status") != "running":
            return  # A late thread must not regress a durable terminal state.
        events = [item for item in events if item.get("id") != event["id"]]
        extra["compaction_progress"] = [*events, stored][-_PROGRESS_RETENTION_LIMIT:]
        session.extra_data = extra
        db.commit()
        # Post-turn work has no open SSE writer. Store its timeline entry on
        # the finished assistant row too, so old history pages do not depend
        # on the bounded session observation list.
        if stored["message_id"] and event.get("phase") == "post_turn":
            message = (
                db.query(ChatMessage)
                .filter_by(message_id=stored["message_id"], chat_id=event["chat_id"])
                .with_for_update()
                .first()
            )
            if message is not None:
                metadata = dict(message.extra_data or {})
                segments = list(metadata.get("segments") or [])
                if not segments:
                    segments = [
                        {"type": "tool", "index": i}
                        for i, _ in enumerate(metadata.get("tool_calls") or [])
                    ]
                    if message.content:
                        segments.append({"type": "text", "text": message.content})
                index = next(
                    (
                        i
                        for i, item in enumerate(segments)
                        if item.get("type") == "compaction"
                        and item.get("progress", {}).get("id") == stored["id"]
                    ),
                    -1,
                )
                step = {"type": "compaction", "progress": stored}
                if index >= 0:
                    previous = segments[index]["progress"]
                    if stored["status"] == "running" and previous.get("status") != "running":
                        return
                    segments[index] = step
                else:
                    last_tool = max(
                        (i for i, item in enumerate(segments) if item.get("type") == "tool"),
                        default=-1,
                    )
                    segments.insert(last_tool + 1, step)
                metadata["segments"] = segments
                message.extra_data = metadata
                db.commit()


def read_progress(chat_service, chat_id: str) -> list[dict]:
    # Access is checked by the route before this projection is read.
    session = chat_service.session_repo.get_by_id(chat_id)
    extra = getattr(session, "extra_data", None) or {}
    result = []
    now = int(time.time() * 1000)
    for item in extra.get("compaction_progress") or []:
        event = dict(item)
        if event.get("status") == "running" and now > event.get("expires_at", now):
            event.update(status="interrupted", ended_at=event["expires_at"], reason="expired")
        result.append(event)
    return result


async def _publish(event: dict) -> None:
    scoped_chat_id, scoped_run_id = _identity.get()
    if not event.get("run_id") and event.get("chat_id") == scoped_chat_id:
        event = {**event, "run_id": scoped_run_id}
    context = _context.get() or {}
    if (
        context.get("message_id")
        and event.get("chat_id") == scoped_chat_id
        and event.get("run_id") == scoped_run_id
    ):
        event = {**event, "message_id": context["message_id"]}
    try:
        await asyncio.to_thread(persist_progress, event)
    except Exception:
        logger.warning("compaction_progress_persist_failed", exc_info=True)
    sink = _sink.get()
    if sink is not None:
        await sink(dict(event))


async def start_progress(chat_id: str, run_id: str, phase: str) -> dict:
    from core.config.settings import settings

    now = int(time.time() * 1000)
    event = {
        "type": "compaction_progress",
        "id": uuid.uuid4().hex,
        "chat_id": chat_id,
        "run_id": run_id,
        "phase": phase,
        "status": "running",
        "started_at": now,
        "expires_at": now
        + (int(settings.compaction.summarize_timeout_s) + _PROGRESS_EXPIRY_GRACE_SECONDS) * 1000,
    }
    await _publish(event)
    return event


async def finish_progress(event: dict, status: str, reason: str = "") -> None:
    await _publish(
        {**event, "status": status, "reason": reason, "ended_at": int(time.time() * 1000)}
    )


async def report_outcome(chat_id: str, run_id: str, phase: str, status: str, reason: str) -> None:
    # Terminal outcomes without summary work should never briefly spin.
    now = int(time.time() * 1000)
    await _publish(
        {
            "type": "compaction_progress",
            "id": uuid.uuid4().hex,
            "chat_id": chat_id,
            "run_id": run_id,
            "phase": phase,
            "status": status,
            "reason": reason,
            "started_at": now,
            "ended_at": now,
        }
    )
