"""Chat sessions and messages behind one service facade."""
from __future__ import annotations
from core.chat import session_operations
from core.chat import message_writes
from core.chat import message_queries
from core.chat import compaction_snapshots
from core.chat import compaction_checkpoints
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
from core.auth.edition import ChatAccessLevel
from core.db.models import ChatCompactionState, ChatMessage, ChatSession
from core.db.repository import AuditLogRepository, ChatMessageRepository, ChatSessionRepository
from sqlalchemy.orm import Session
import logging
from core.chat import compaction_types

logger = logging.getLogger(__name__)

class ChatService:
    """Service for chat-related operations."""

    def __init__(self, db: Session):
        self.db = db
        self.session_repo = ChatSessionRepository(db)
        self.message_repo = ChatMessageRepository(db)
        self.audit_repo = AuditLogRepository(db)

    def create_session(
        self,
        user_id: str,
        title: str = "新对话",
        extra_data: Dict = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> ChatSession:
        return session_operations.create_session(self, user_id, title, extra_data, chat_id, project_id)

    def ensure_session(
        self,
        chat_id: str,
        user_id: str,
        title: str = "新对话",
        extra_data: Optional[Dict] = None,
        project_id: Optional[str] = None,
    ) -> Optional[ChatSession]:
        return session_operations.ensure_session(self, chat_id, user_id, title, extra_data, project_id)

    def list_sessions(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        pinned_only: bool = False,
        favorite_only: bool = False,
        exclude_automation: bool = False,
    ) -> Tuple[List[ChatSession], int, int]:
        return session_operations.list_sessions(self, user_id, page, page_size, pinned_only, favorite_only, exclude_automation)

    def get_session(self, chat_id: str, user_id: str) -> Optional[ChatSession]:
        return session_operations.get_session(self, chat_id, user_id)

    def get_session_with_access(
        self, chat_id: str, user_id: str
    ) -> Optional[Tuple[ChatSession, ChatAccessLevel]]:
        return session_operations.get_session_with_access(self, chat_id, user_id)

    def update_session_fields(
        self,
        chat_id: str,
        fields: Dict[str, Any],
        *,
        actor_user_id: Optional[str] = None,
    ) -> Optional[ChatSession]:
        return session_operations.update_session_fields(self, chat_id, fields, actor_user_id=actor_user_id)

    def delete_session_force(self, chat_id: str, *, actor_user_id: str) -> bool:
        return session_operations.delete_session_force(self, chat_id, actor_user_id=actor_user_id)

    def update_session(
        self, chat_id: str, user_id: str, update_data: Dict[str, Any]
    ) -> Optional[ChatSession]:
        return session_operations.update_session(self, chat_id, user_id, update_data)

    def delete_session(self, chat_id: str, user_id: str) -> bool:
        return session_operations.delete_session(self, chat_id, user_id)

    # chat_messages.content 有 CHECK 约束 char_length <= 100000。长任务的一轮回复
    # （反复重试 + 大量思考正文）真的会撞上，而撞上的后果是**整条 INSERT 抛
    # CheckViolation → 整个 run failed**，一轮的产出全部丢失（实测踩过）。
    # 与其让约束把一轮工作全毁掉，不如夹逼后落库并明确标注被截断。
    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        model: Optional[str] = None,
        thinking: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List[Dict]] = None,
        usage: Optional[Dict] = None,
        error: Optional[Dict] = None,
        extra_data: Dict = None,
        message_id: Optional[str] = None,
        commit: bool = True,
        chat_seq: Optional[int] = None,
        model_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatMessage:
        return message_writes.add_message(self, chat_id, role, content, model, thinking, tool_calls, usage, error, extra_data, message_id, commit, chat_seq, model_steps)

    def refresh_streaming_message(
        self,
        *,
        run_id: str,
        owner: str,
        message_id: str,
        content: str,
        thinking: Optional[List[Dict[str, Any]]],
        tool_calls: Optional[List[Dict]],
        extra_data: Dict[str, Any],
        model_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        return message_writes.refresh_streaming_message(self, run_id=run_id, owner=owner, message_id=message_id, content=content, thinking=thinking, tool_calls=tool_calls, extra_data=extra_data, model_steps=model_steps)

    def upsert_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        message_id: str,
        model: Optional[str] = None,
        thinking: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List[Dict]] = None,
        usage: Optional[Dict] = None,
        error: Optional[Dict] = None,
        extra_data: Dict = None,
        commit: bool = True,
        chat_seq: Optional[int] = None,
        model_steps: Optional[List[Dict[str, Any]]] = None,
        invalidate_mid_turn_checkpoint: bool = False,
    ) -> ChatMessage:
        return message_writes.upsert_message(self, chat_id, role, content, message_id=message_id, model=model, thinking=thinking, tool_calls=tool_calls, usage=usage, error=error, extra_data=extra_data, commit=commit, chat_seq=chat_seq, model_steps=model_steps, invalidate_mid_turn_checkpoint=invalidate_mid_turn_checkpoint)

    def list_all_messages(
        self, chat_id: str, user_id: str
    ) -> Optional[List[ChatMessage]]:
        return message_queries.list_all_messages(self, chat_id, user_id)

    def list_messages(
        self, chat_id: str, user_id: str, page: int = 1, page_size: int = 50
    ) -> Optional[Tuple[List[ChatMessage], int, int]]:
        return message_queries.list_messages(self, chat_id, user_id, page, page_size)

    def delete_messages_from(self, chat_id: str, message_id: str) -> int:
        return message_writes.delete_messages_from(self, chat_id, message_id)

    def _invalidate_compaction_lineage_for_rewrite(
        self,
        chat_id: str,
        *,
        rewritten_seq: int,
    ) -> bool:
        return compaction_checkpoints.invalidate_compaction_lineage_for_rewrite(self, chat_id, rewritten_seq=rewritten_seq)

    def add_compaction_checkpoint(
        self,
        chat_id: str,
        *,
        summary_text: str,
        replacement_history: List[Dict],
        covered_seq: Optional[int] = None,
        source_hash: Optional[str] = None,
        base_checkpoint_version: Optional[int] = None,
        replacement_manifest: Optional[Dict[str, Any]] = None,
    ) -> ChatMessage:
        return compaction_checkpoints.add_compaction_checkpoint(self, chat_id, summary_text=summary_text, replacement_history=replacement_history, covered_seq=covered_seq, source_hash=source_hash, base_checkpoint_version=base_checkpoint_version, replacement_manifest=replacement_manifest)

    def _latest_compaction_checkpoint_fallback(
        self, chat_id: str
    ) -> Optional[ChatMessage]:
        return compaction_checkpoints.latest_compaction_checkpoint_fallback(self, chat_id)

    @staticmethod
    def _checkpoint_covered_seq(checkpoint: Optional[ChatMessage]) -> int:
        return compaction_checkpoints.checkpoint_covered_seq(checkpoint)

    def _ensure_message_sequences(self, chat_id: str) -> None:
        return compaction_snapshots.ensure_message_sequences(self, chat_id)

    def _ensure_compaction_state(self, chat_id: str) -> ChatCompactionState:
        return compaction_snapshots.ensure_compaction_state(self, chat_id)

    def acquire_compaction_snapshot(
        self,
        chat_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> Optional[compaction_types.CompactionSnapshot]:
        return compaction_snapshots.acquire_compaction_snapshot(self, chat_id, owner=owner, lease_seconds=lease_seconds)

    def _acquire_compaction_snapshot(
        self,
        chat_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> Optional[compaction_types.CompactionSnapshot]:
        return compaction_snapshots.acquire_compaction_snapshot_impl(self, chat_id, owner=owner, lease_seconds=lease_seconds)

    def release_compaction_lease(self, chat_id: str, *, owner: str) -> bool:
        return compaction_snapshots.release_compaction_lease(self, chat_id, owner=owner)

    def commit_compaction_checkpoint(
        self,
        snapshot: compaction_types.CompactionSnapshot,
        *,
        owner: str,
        summary_text: str,
        replacement_history: List[Dict[str, Any]],
        replacement_manifest: Dict[str, Any],
    ) -> ChatMessage:
        return compaction_checkpoints.commit_compaction_checkpoint(self, snapshot, owner=owner, summary_text=summary_text, replacement_history=replacement_history, replacement_manifest=replacement_manifest)

    def get_latest_compaction_checkpoint(self, chat_id: str) -> Optional[ChatMessage]:
        return compaction_checkpoints.get_latest_compaction_checkpoint(self, chat_id)

    def get_message_by_id(self, message_id: str) -> Optional[ChatMessage]:
        return message_queries.get_message_by_id(self, message_id)

    def get_message_by_index(self, chat_id: str, index: int) -> Optional[ChatMessage]:
        return message_queries.get_message_by_index(self, chat_id, index)

    def get_user_message_before(
        self, chat_id: str, message_id: str
    ) -> Optional[ChatMessage]:
        return message_queries.get_user_message_before(self, chat_id, message_id)

    def update_message_extra_data(
        self,
        message_id: str,
        patch: Dict[str, Any],
    ) -> bool:
        return message_writes.update_message_extra_data(self, message_id, patch)

    def accept_ontology_revision(self, message_id: str) -> Optional[ChatMessage]:
        return message_writes.accept_ontology_revision(self, message_id)

    def search_sessions(
        self,
        user_id: str,
        query: str,
        page: int = 1,
        page_size: int = 20,
        scope: str = "title",
    ) -> Tuple[list, int]:
        return session_operations.search_sessions(self, user_id, query, page, page_size, scope)
