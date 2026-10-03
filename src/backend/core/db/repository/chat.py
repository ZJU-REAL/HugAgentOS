"""Data access layer — chat repositories.

Split out of the former monolithic ``core/db/repository.py``. The package
``__init__`` re-exports every repository class, so ``from core.db.repository
import XxxRepository`` keeps working unchanged.
"""
from core.infra.time import utc_now

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from core.db.models import ChatMessage, ChatSession
from core.db.models.chat import reserve_chat_message_sequences
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session


def _strip_nul(value: Any) -> Any:
    """递归剥离 ``\\u0000``——PostgreSQL 的 text/JSONB 不接受 NUL 字符。

    工具结果里混入字面 NUL 并不罕见（PDF 提取文本、二进制味的网页正文），
    一旦原样进入 content / tool_calls 等字段，INSERT 会被
    ``UntranslatableCharacter`` 整条拒绝、消息丢失。这里是消息落库的唯一
    收口，普通对话 / 定时任务 / 自主循环全部经过。
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul(v) for v in value)
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    return value




class ChatSessionRepository:
    """Repository for chat session operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, chat_id: str) -> Optional[ChatSession]:
        """Get chat session by ID."""
        return (
            self.db.query(ChatSession)
            .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
            .first()
        )

    def list_by_user(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        pinned_only: bool = False,
        favorite_only: bool = False,
        exclude_automation: bool = False,
    ) -> tuple[List[ChatSession], int]:
        """List chat sessions for a user with pagination."""
        query = self.db.query(ChatSession).filter(
            ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None)
        )

        if pinned_only:
            query = query.filter(ChatSession.pinned == True)
        if favorite_only:
            query = query.filter(ChatSession.favorite == True)
        if exclude_automation:
            # Exclude sessions created by automation scheduler.
            # extra_data is mapped to the "metadata" JSON column.
            # Use dialect-portable cast: check the JSON text doesn't contain the marker key.
            query = query.filter(
                or_(
                    ChatSession.extra_data.is_(None),
                    ~func.cast(ChatSession.extra_data, sa.Text).contains(
                        '"automation_run"'
                    ),
                )
            )

        # Get total count
        total = query.count()

        # Apply pagination and ordering
        sessions = (
            query.order_by(desc(ChatSession.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return sessions, total

    def create(self, session_data: Dict[str, Any]) -> ChatSession:
        """Create a new chat session."""
        session = ChatSession(**session_data)
        session.created_at = utc_now()
        session.updated_at = utc_now()
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def update(
        self, chat_id: str, update_data: Dict[str, Any]
    ) -> Optional[ChatSession]:
        """Update chat session."""
        session = self.get_by_id(chat_id)
        if not session:
            return None

        for key, value in update_data.items():
            setattr(session, key, value)

        session.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(session)
        return session

    def soft_delete(self, chat_id: str) -> bool:
        """Soft delete a chat session."""
        session = self.get_by_id(chat_id)
        if not session:
            return False

        session.deleted_at = utc_now()
        self.db.commit()
        return True

    def search(
        self,
        user_id: str,
        query: str,
        page: int = 1,
        page_size: int = 20,
        scope: str = "title",
    ) -> tuple[List[Dict[str, Any]], int]:
        from core.db.chat_search import search_sessions

        return search_sessions(self.db, user_id, query, page, page_size, scope)


class ChatMessageRepository:
    """Repository for chat message operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, message_id: str) -> Optional[ChatMessage]:
        """Get message by ID."""
        return (
            self.db.query(ChatMessage)
            .filter(ChatMessage.message_id == message_id)
            .first()
        )

    def list_by_chat(
        self,
        chat_id: str,
        page: int = 1,
        page_size: int = 50,
        *,
        newest_first: bool = False,
        display_only: bool = False,
    ) -> tuple[List[ChatMessage], int]:
        """List messages for a chat session with pagination.

        Excludes role='system' rows — these include compaction checkpoints (internal
        artifacts, not visible to the user).

        ``newest_first`` 让第 1 页是**最近**的一批。前端打开会话时要的正是这个：
        先拿最近 N 条渲染出来，用户往上滚再要第 2、3 页。按正序分页做不到这件事——
        不先跑一趟拿总数就不知道最后一页是第几页。返回的这一页内部仍是时间正序，
        调用方直接往列表前面拼即可。
        """
        query = self.db.query(ChatMessage).filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role != "system",
        )

        total = query.count()
        if display_only:
            from core.db.history_page import display_query
            query = display_query(self.db, chat_id)
        order = ChatMessage.chat_seq.desc() if newest_first else ChatMessage.chat_seq
        messages = (
            query.order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        if newest_first:
            messages.reverse()

        return messages, total

    def list_recent_by_chat(
        self,
        chat_id: str,
        *,
        limit: int = 8,
        before_seq: Optional[int] = None,
        metadata_only: bool = False,
    ) -> List[ChatMessage]:
        """Return the newest visible messages in chronological order.

        ``before_seq`` makes a post-response consumer stop at the assistant
        message it belongs to, so a later overlapping turn cannot leak in.
        """

        selected = (ChatMessage.chat_seq, ChatMessage.extra_data) if metadata_only else (ChatMessage,)
        query = self.db.query(*selected).filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role.in_(("user", "assistant")),
        )
        if before_seq is not None:
            query = query.filter(ChatMessage.chat_seq <= before_seq)
        messages = query.order_by(ChatMessage.chat_seq.desc()).limit(max(1, limit)).all()
        messages.reverse()
        return messages

    def count_visible_through_seq(self, chat_id: str, covered_seq: int) -> int:
        """Count user-facing messages included in a compaction watermark.

        Compaction checkpoints use ``role='system'`` and are intentionally
        excluded, matching :meth:`list_by_chat`.  The count gives clients a
        stable sequence boundary in the visible history without exposing the
        checkpoint row or its replacement payload.
        """
        return int(
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role != "system",
                ChatMessage.chat_seq <= covered_seq,
            )
            .count()
        )

    def count_visible_through_seq(self, chat_id: str, covered_seq: int) -> int:
        """Count user-facing rows at or below an exact compaction watermark."""
        return int(
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role != "system",
                ChatMessage.chat_seq <= int(covered_seq),
            )
            .count()
        )

    def create(
        self, message_data: Dict[str, Any], *, commit: bool = True
    ) -> ChatMessage:
        """Create a new chat message."""
        clean = _strip_nul(message_data)
        if clean.get("chat_seq") is None:
            next_seq = reserve_chat_message_sequences(
                self.db, str(clean.get("chat_id") or ""), 1
            )
            if next_seq is not None:
                clean["chat_seq"] = next_seq

        message = ChatMessage(**clean)
        message.created_at = utc_now()
        self.db.add(message)
        if commit:
            self.db.commit()
            self.db.refresh(message)
        else:
            self.db.flush()
        return message

    def update(
        self,
        message_id: str,
        update_data: Dict[str, Any],
        *,
        commit: bool = True,
    ) -> Optional[ChatMessage]:
        """Update mutable fields (content / tool_calls / usage / extra_data / …) in place.

        Used for scenarios like the autonomous loop where "the same assistant message is
        incrementally refreshed as progress advances" — overwrites in place by message_id,
        without adding a new row. Returns None for an unknown message_id.
        """
        message = self.get_by_id(message_id)
        if not message:
            return None
        for key, value in update_data.items():
            setattr(message, key, _strip_nul(value))
        if commit:
            self.db.commit()
            self.db.refresh(message)
        else:
            self.db.flush()
        return message

    def update_extra_data(
        self, message_id: str, patch: Dict[str, Any]
    ) -> Optional[ChatMessage]:
        """Merge *patch* into the message's extra_data JSONB field."""
        message = self.get_by_id(message_id)
        if not message:
            return None
        merged = dict(message.extra_data or {})
        merged.update(_strip_nul(patch))
        message.extra_data = merged
        self.db.commit()
        self.db.refresh(message)
        return message
