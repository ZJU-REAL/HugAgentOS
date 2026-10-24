"""Read chat messages, with access checks."""
from __future__ import annotations
from typing import List, Optional, Tuple
from core.db.models import ChatMessage

def list_all_messages(
    self, chat_id: str, user_id: str
) -> Optional[List[ChatMessage]]:
    """List all messages in chronological order with access check.

    Edition-specific sharing policies may also grant read access.

    **Excludes** compaction checkpoint rows (the only writer of role='system' in
    chat_messages is add_compaction_checkpoint) — internal artifacts, invisible to all
    downstream consumers (replay/export/sharing/title/memory), filtered at the SQL layer
    (a checkpoint's extra_data carries the entire replacement_history; loading it just to
    discard is pure waste). The replay layer (compaction_service) fetches them separately
    via get_latest_compaction_checkpoint.
    """
    pair = self.get_session_with_access(chat_id, user_id)
    if pair is None:
        return None

    return (
        self.db.query(ChatMessage)
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role != "system",
        )
        .order_by(ChatMessage.chat_seq)
        .all()
    )


def list_messages(
    self, chat_id: str, user_id: str, page: int = 1, page_size: int = 50
) -> Optional[Tuple[List[ChatMessage], int, int]]:
    """List messages in a chat session."""
    # Check ownership
    session = self.get_session(chat_id, user_id)
    if not session:
        return None

    messages, total = self.message_repo.list_by_chat(chat_id, page, page_size)
    total_pages = (total + page_size - 1) // page_size

    return messages, total, total_pages


def get_message_by_id(self, message_id: str) -> Optional[ChatMessage]:
    """Get a single message by its ID."""
    return (
        self.db.query(ChatMessage)
        .filter(
            ChatMessage.message_id == message_id,
        )
        .first()
    )


def get_message_by_index(self, chat_id: str, index: int) -> Optional[ChatMessage]:
    """Get a message by its durable 0-based position in the chat."""
    return (
        self.db.query(ChatMessage)
        .filter(
            ChatMessage.chat_id == chat_id,
        )
        .order_by(ChatMessage.chat_seq)
        .offset(index)
        .limit(1)
        .first()
    )


def get_user_message_before(
    self, chat_id: str, message_id: str
) -> Optional[ChatMessage]:
    """Get the user message immediately before the given message."""
    target = self.get_message_by_id(message_id)
    if not target:
        return None
    return (
        self.db.query(ChatMessage)
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role == "user",
            ChatMessage.chat_seq < target.chat_seq,
        )
        .order_by(ChatMessage.chat_seq.desc())
        .first()
    )
