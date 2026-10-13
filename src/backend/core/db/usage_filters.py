"""Shared predicate for model-call reports over persisted chat messages."""

from core.db.models import ChatMessage


def model_usage_message_filter():
    """History snapshots are not additional calls, even when they show old usage."""
    return (ChatMessage.role == "assistant") & ChatMessage.usage_is_history.is_(False)
