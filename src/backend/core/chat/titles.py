"""One non-blocking first-question title call, with durable claim and rename fencing."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field

from core.db import engine as db_engine
from core.db.models import ChatMessage, ChatSession
from core.models.completions.summarizer import get_summarizer

logger = logging.getLogger(__name__)


def _claim(chat_id: str, user_id: str, run_id: str) -> str | None:
    with db_engine.SessionLocal() as db:
        row = db.query(ChatSession).filter_by(chat_id=chat_id, user_id=user_id).first()
        if row is None or row.deleted_at is not None:
            return None
        meta = dict(row.extra_data or {})
        if any(meta.get(key) for key in (
            "title_manually_set", "title_generation", "fork", "automation_run",
            "public_chat_title_set",
        )):
            return None
        messages = db.query(ChatMessage).filter_by(chat_id=chat_id, role="user").order_by(
            ChatMessage.chat_seq
        ).limit(2).all()
        if len(messages) != 1:
            return None
        question = messages[0].content.strip()
        if not question or question in ("/init", "/初始化指令", "/compact"):
            return None
        claimed = {**meta, "title_generation": {"run_id": run_id, "status": "pending", "fallback": row.title}}
        changed = db.query(ChatSession).filter(
            ChatSession.chat_id == chat_id,
            ChatSession.user_id == user_id,
            ChatSession.title == row.title,
            ChatSession.extra_data == row.extra_data,
            ChatSession.deleted_at.is_(None),
        ).update({ChatSession.extra_data: claimed, ChatSession.updated_at: ChatSession.updated_at},
                 synchronize_session=False)
        db.commit()
        return question if changed else None


def _finish(chat_id: str, user_id: str, run_id: str, title: str | None) -> None:
    # Metadata may change while the auxiliary model is running. Merge a fresh
    # snapshot and compare-and-swap it so a simultaneous rename always wins.
    for _ in range(3):
        with db_engine.SessionLocal() as db:
            row = db.query(ChatSession).filter_by(chat_id=chat_id, user_id=user_id).first()
            if row is None or row.deleted_at is not None:
                return
            meta = dict(row.extra_data or {})
            generation = meta.get("title_generation") or {}
            if generation.get("run_id") != run_id or generation.get("status") != "pending":
                return
            manual = bool(meta.get("title_manually_set")) or row.title != generation.get("fallback")
            status = "superseded" if manual else "completed" if title else "failed"
            updated = {**meta, "title_generation": {**generation, "status": status}}
            values = {ChatSession.extra_data: updated, ChatSession.updated_at: ChatSession.updated_at}
            if title and not manual:
                values[ChatSession.title] = title
            changed = db.query(ChatSession).filter(
                ChatSession.chat_id == chat_id,
                ChatSession.user_id == user_id,
                ChatSession.title == row.title,
                ChatSession.extra_data == row.extra_data,
                ChatSession.deleted_at.is_(None),
            ).update(values, synchronize_session=False)
            db.commit()
            if changed:
                return
    logger.warning("chat_title_update_conflict", extra={"chat_id": chat_id})


async def _generate(chat_id: str, user_id: str, run_id: str, question: str) -> None:
    title = None
    try:
        async with asyncio.timeout(30):
            result = await get_summarizer().summarize_conversation(
                [{"role": "user", "content": question[:8000]}]
            )
        if result:
            title = " ".join(result.split())[:30]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not expose the user's prompt, model output or provider credentials.
        logger.warning("chat_title_generation_failed: %s", type(exc).__name__)
    finally:
        _finish(chat_id, user_id, run_id, title)


def schedule_first_title(chat_id: str, user_id: str, run_id: str) -> asyncio.Task | None:
    """Return immediately after claiming a first-message title; never await the model."""
    if not get_summarizer().enabled:
        return None
    question = _claim(chat_id, user_id, run_id)
    if question is None:
        return None
    task = asyncio.create_task(_generate(chat_id, user_id, run_id, question), name=f"chat-title:{chat_id}")
    process_state().tasks.add(task)
    def settled(done: asyncio.Task) -> None:
        process_state().tasks.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.warning("chat_title_task_failed: %s", type(done.exception()).__name__)

    task.add_done_callback(settled)
    return task


@dataclass
class TitlesState:
    tasks: set[asyncio.Task] = field(default_factory=lambda: set())


_STATE = TitlesState()


def process_state() -> TitlesState:
    return _STATE
