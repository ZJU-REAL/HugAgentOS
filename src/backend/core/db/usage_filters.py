"""Shared predicate for model-call reports over persisted chat messages."""

from core.db.models import ChatMessage
from sqlalchemy import func


def model_usage_message_filter():
    """History snapshots are not additional calls, even when they show old usage."""
    return (ChatMessage.role == "assistant") & func.coalesce(
        ChatMessage.extra_data["forked_history"].as_boolean(), False
    ).is_(False)
