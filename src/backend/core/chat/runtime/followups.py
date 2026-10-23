"""Schedule post-turn suggestions and drain admitted follow-up work."""

from __future__ import annotations

import asyncio
import typing as _typing
from typing import Any, Dict, List, Optional, Tuple

from core.db.threads import run_in_session
import core.runs.settings as settings
import core.chat.service as services_ChatService_module
from core.infra.logging import get_logger
from core.runs.events.store import Entry

logger = get_logger(__name__)


def _spawn_followup_task(
    *, run_id: str, chat_id: str, user_msg: str, response: str, msg_id: str
) -> None:
    """Equivalent to the existing _generate_followups_bg in chats.py."""
    from core.chat.followups import get_followup_generator

    async def _bg() -> None:
        try:
            questions = await asyncio.wait_for(
                get_followup_generator().generate(user_msg, response, run_id=run_id),
                timeout=10,
            )
            if questions:
                await run_in_session(
                    lambda db: services_ChatService_module.ChatService(db).update_message_extra_data(
                        msg_id, {"follow_up_questions": questions}
                    )
                )
        except Exception as exc:
            logger.warning("background_followup_failed", error=str(exc))

    asyncio.create_task(_bg())


def _drain(
    entries: Optional[List[Entry]], from_offset: int, cursor: str
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Split a batch into deliverable events, the new cursor, and "saw terminal".

    Skips ``model_progress`` markers: nothing writes them anymore (the reaper
    trusts in-process task liveness), but entries from older builds may survive
    within the log's TTL and must never reach clients — the frontend treats an
    unknown event type as "pending ended".

    An empty batch may arrive as ``None`` rather than ``[]``; a read that
    returns nothing must never surface to the client as "流式响应中断".
    """
    delivered: _typing.List[_typing.Dict[str, _typing.Any]] = []
    for entry_cursor, event in entries or ():
        cursor = entry_cursor
        if not isinstance(event, dict) or event.get("type") == "model_progress":
            continue
        if event.get("type") == settings.TERMINAL_TYPE:
            return delivered, cursor, True
        if event.get("_offset", 0) > from_offset:
            delivered.append(event)
    return delivered, cursor, False
