"""Ownership and interrupted-run checks shared by pending interactions."""

from datetime import datetime

import api.routes.v1.chats.session_context as chat_session_context
import core.services as chat_services
from sqlalchemy.orm import Session


def _detect_chat_run_interrupted(db: Session, chat_id: str) -> bool:
    """Detect whether the chat's most recent run failed due to a server restart (within 30 minutes).

    Used to distinguish whether a ``/file-confirm`` stale result is an ordinary timeout reclaim,
    or a server restart that killed the whole agent task — in the latter case the user must resend
    the message, and a timeout-style hint would be very confusing.

    Returns False on any exception (stays compatible with the legacy stale path).
    """
    try:
        from datetime import timedelta, timezone

        from core.db.models import ChatRun

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        recent = (
            db.query(ChatRun.error_message)
            .filter(ChatRun.chat_id == chat_id)
            .filter(ChatRun.status == "failed")
            .filter(ChatRun.completed_at > cutoff)
            .order_by(ChatRun.completed_at.desc())
            .first()
        )
        if recent is None:
            return False
        err = (recent.error_message or "").lower()
        return "server restarted" in err or "server_restart" in err
    except (
        Exception
    ):  # noqa: BLE001 — on detection failure fall back to the old path; must not affect the main confirm flow
        return False


def _owns_pending_chat(db, chat_id, user_id):
    try:
        return chat_services.ChatService(db).get_session(chat_id, user_id) is not None
    finally:
        chat_session_context._release_request_session(db)
