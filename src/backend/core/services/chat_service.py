"""Chat session and message business logic."""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.auth.permissions_iface import (
    ChatAccessLevel,
    can_delete_session,
    resolve_chat_access,
)
from core.db.models import ChatCompactionState, ChatMessage, ChatRun, ChatSession
from core.db.repository import (
    AuditLogRepository,
    ChatMessageRepository,
    ChatSessionRepository,
)
from core.db.repository.chat import _strip_nul
from sqlalchemy import exists, update
from core.ontology.revision import is_substantive_revision, normalize_revision_candidate
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CONTENT_MAX = 100_000
_TRUNC_NOTE = "\n\n…（本条回复过长，已截断保存；完整过程见工具调用记录）"
# 思考的独立额度。content 有 CHECK 约束卡死 10 万字，thinking 是 JSON 列没有
# 约束，但也不能无限长——跑飞的一轮能产出几十万字推理。
_THINKING_MAX = 200_000


def clamp_message_content(content: str) -> str:
    """Apply the database content limit independently of ChatService wiring."""

    text = content or ""
    if len(text) <= _CONTENT_MAX:
        return text
    keep = _CONTENT_MAX - len(_TRUNC_NOTE)
    logger.warning("[chat] message content truncated: %d -> %d chars", len(text), _CONTENT_MAX)
    kept = text[:keep]
    # 老格式的思考是以 <think>…</think> 内联在正文里的。截断点可能落在块中间，
    # 闭合标签一丢，历史重建就把整段思考当正文渲染出来（思考泄露到页面上）。
    close = "</think>"
    if kept.count("<think>") > kept.count(close):
        kept = kept[: keep - len(close)] + close
    return kept + _TRUNC_NOTE


def clamp_thinking(
    blocks: Optional[List[Dict[str, Any]]],
    segments: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
    """思考单独存一列，有自己的额度，不再和正文抢 content 的 10 万字上限。

    超额时从最早的块开始丢——最后几轮的推理离最终答案最近，对用户回看最有用。

    ``metadata.segments`` 按下标引用思考块，丢块会让下标错位，所以两者必须一起夹取：
    返回夹取后的块列表，以及下标已重映射、引用了被丢块的段落已剔除的段落表。
    """
    if not blocks:
        return None, segments or None
    if sum(len(str(b.get("content") or "")) for b in blocks) <= _THINKING_MAX:
        return blocks, segments or None
    kept: List[Dict[str, Any]] = []
    kept_indices: List[int] = []
    budget = _THINKING_MAX
    for offset, block in enumerate(reversed(blocks)):
        text = str(block.get("content") or "")
        if not text:
            continue
        if len(text) > budget:
            text = text[len(text) - budget :]
        if not text:
            break
        budget -= len(text)
        kept.append({**block, "content": text})
        kept_indices.append(len(blocks) - 1 - offset)
        if budget <= 0:
            break
    kept.reverse()
    kept_indices.reverse()
    if not kept:
        return None, _drop_thinking_segments(segments, {})
    remap = {old: new for new, old in enumerate(kept_indices)}
    return kept, _drop_thinking_segments(segments, remap)


def _drop_thinking_segments(
    segments: Optional[List[Dict[str, Any]]],
    remap: Dict[int, int],
) -> Optional[List[Dict[str, Any]]]:
    """把段落表里的思考下标重映射到夹取后的位置，引用已丢块的段落直接剔除。"""
    if not segments:
        return segments or None
    rebuilt: List[Dict[str, Any]] = []
    for segment in segments:
        if segment.get("type") != "thinking":
            rebuilt.append(segment)
            continue
        new_index = remap.get(segment.get("index"))
        if new_index is None:
            continue
        rebuilt.append({**segment, "index": new_index})
    return rebuilt or None


class CompactionCASConflict(RuntimeError):
    """The checkpoint base/lease is stale and another writer won."""


@dataclass(frozen=True)
class CompactionSourceRow:
    """Detached, immutable copy of one message in the compacted snapshot."""

    message_id: str
    chat_seq: int
    role: str
    content: str
    tool_calls: Any
    extra_data: Dict[str, Any]


@dataclass(frozen=True)
class CompactionSnapshot:
    """Immutable source boundary captured before the summary LLM call."""

    chat_id: str
    owner: str
    base_checkpoint_id: Optional[str]
    base_checkpoint_version: int
    base_covered_seq: int
    covered_seq: int
    source_hash: str
    replacement_history: tuple[Dict[str, Any], ...]
    source_rows: tuple[CompactionSourceRow, ...]
    source_message_ids: tuple[str, ...]
    source_message_seqs: tuple[int, ...]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    ) -> ChatSession:
        """Create a new chat session.

        If `chat_id` is provided and belongs to the same user, reuse it.
        If it belongs to another user, generate a new chat_id.
        """
        if chat_id:
            existing = self.session_repo.get_by_id(chat_id)
            if existing:
                if existing.user_id == user_id:
                    return existing
                chat_id = None

        session_data = {
            "chat_id": chat_id or f"chat_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "title": title,
            "extra_data": extra_data or {},
        }
        session = self.session_repo.create(session_data)

        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "chat.session.created",
                "resource_type": "chat_session",
                "resource_id": session.chat_id,
                "status": "success",
            }
        )

        return session

    def ensure_session(
        self,
        chat_id: str,
        user_id: str,
        title: str = "新对话",
        extra_data: Optional[Dict] = None,
        project_id: Optional[str] = None,
    ) -> Optional[ChatSession]:
        """Ensure a chat session exists for user and chat_id.

        When ``project_id`` is given, attach the session to that project (first write wins; if
        already attached to a different project it stays unchanged — in-project chats never drift
        across projects). All session-creation entry points share this attachment rule.
        """
        existing = self.session_repo.get_by_id(chat_id)
        if existing:
            if existing.user_id != user_id:
                return None
            # Merge any missing metadata flags into existing session
            if extra_data:
                merged = dict(existing.extra_data or {})
                changed = False
                for k, v in extra_data.items():
                    if k not in merged:
                        merged[k] = v
                        changed = True
                if changed:
                    existing.extra_data = merged
                    self.db.commit()
            session = existing
        else:
            session = self.create_session(
                user_id=user_id,
                title=title,
                extra_data=extra_data or {},
                chat_id=chat_id,
            )
        if session is not None and project_id and not session.project_id:
            session.project_id = project_id
            self.db.commit()
        return session

    def list_sessions(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        pinned_only: bool = False,
        favorite_only: bool = False,
        exclude_automation: bool = False,
    ) -> Tuple[List[ChatSession], int, int]:
        """List chat sessions with pagination."""
        sessions, total = self.session_repo.list_by_user(
            user_id,
            page,
            page_size,
            pinned_only,
            favorite_only,
            exclude_automation=exclude_automation,
        )

        total_pages = (total + page_size - 1) // page_size

        return sessions, total, total_pages

    def get_session(self, chat_id: str, user_id: str) -> Optional[ChatSession]:
        """Get chat session with ownership check.

        Historical semantics unchanged: only the owner can get the session. For
        edition-specific sharing, use :py:meth:`get_session_with_access`.
        """
        session = self.session_repo.get_by_id(chat_id)

        if session and session.user_id != user_id:
            # Access denied - user doesn't own this session
            return None

        return session

    def get_session_with_access(
        self, chat_id: str, user_id: str
    ) -> Optional[Tuple[ChatSession, ChatAccessLevel]]:
        """Get a session in a sharing context + compute the access level.

        - Does not exist / soft-deleted → ``None``
        - No access (``resolve_chat_access`` returns ``'none'``) → ``None``
        - Otherwise returns ``(session, 'admin'|'edit'|'read')``
        """
        session = self.session_repo.get_by_id(chat_id)
        if session is None:
            return None
        level = resolve_chat_access(self.db, user_id, session)
        if level == "none":
            return None
        return session, level

    def update_session_fields(
        self,
        chat_id: str,
        fields: Dict[str, Any],
        *,
        actor_user_id: Optional[str] = None,
    ) -> Optional[ChatSession]:
        """Field update without ownership check (for sharing contexts).

        The caller is responsible for permission checks beforehand. ``actor_user_id`` is used
        only for the audit log. The ``extra_data`` field is merged rather than overwritten.
        """
        session = self.session_repo.get_by_id(chat_id)
        if session is None:
            return None
        normalized = dict(fields)
        extra_patch = normalized.get("extra_data")
        if isinstance(extra_patch, dict):
            merged = dict(session.extra_data or {})
            merged.update(extra_patch)
            normalized["extra_data"] = merged
        updated = self.session_repo.update(chat_id, normalized)
        self.audit_repo.create(
            {
                "user_id": actor_user_id or session.user_id,
                "action": "chat.session.updated",
                "resource_type": "chat_session",
                "resource_id": chat_id,
                "details": normalized,
                "status": "success",
            }
        )
        return updated

    def delete_session_force(self, chat_id: str, *, actor_user_id: str) -> bool:
        """Forced delete in a sharing context (no ownership check). Caller handles permissions."""
        session = self.session_repo.get_by_id(chat_id)
        if session is None:
            return False
        result = self.session_repo.soft_delete(chat_id)
        if result:
            self.audit_repo.create(
                {
                    "user_id": actor_user_id,
                    "action": "chat.session.deleted",
                    "resource_type": "chat_session",
                    "resource_id": chat_id,
                    "details": {
                        "owner_user_id": session.user_id,
                        "deleted_by_owner": session.user_id == actor_user_id,
                    },
                    "status": "success",
                }
            )
        return result

    def update_session(
        self, chat_id: str, user_id: str, update_data: Dict[str, Any]
    ) -> Optional[ChatSession]:
        """Update chat session."""
        session = self.get_session(chat_id, user_id)
        if not session:
            return None

        normalized_update_data = dict(update_data)
        extra_data_patch = normalized_update_data.get("extra_data")
        if isinstance(extra_data_patch, dict):
            merged_extra_data = dict(session.extra_data or {})
            merged_extra_data.update(extra_data_patch)
            normalized_update_data["extra_data"] = merged_extra_data

        updated_session = self.session_repo.update(chat_id, normalized_update_data)

        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "chat.session.updated",
                "resource_type": "chat_session",
                "resource_id": chat_id,
                "details": normalized_update_data,
                "status": "success",
            }
        )

        return updated_session

    def delete_session(self, chat_id: str, user_id: str) -> bool:
        """Delete chat session (soft delete)."""
        session = self.get_session(chat_id, user_id)
        if not session:
            return False

        result = self.session_repo.soft_delete(chat_id)

        if result:
            # Audit log
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "chat.session.deleted",
                    "resource_type": "chat_session",
                    "resource_id": chat_id,
                    "status": "success",
                }
            )

        return result

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
    ) -> ChatMessage:
        """Add a message to a chat session."""
        extra = dict(extra_data or {})
        kept_thinking, kept_segments = clamp_thinking(thinking, extra.get("segments"))
        if "segments" in extra:
            extra["segments"] = kept_segments
        message_data = {
            "message_id": message_id or f"msg_{uuid.uuid4().hex[:16]}",
            "chat_id": chat_id,
            "chat_seq": chat_seq,
            "role": role,
            "content": clamp_message_content(content),
            "model": model,
            "thinking": kept_thinking,
            "tool_calls": tool_calls,
            "usage": usage,
            "error": error,
            "extra_data": extra,
        }

        message = self.message_repo.create(message_data, commit=commit)

        # Keep session metadata in sync for list APIs.
        session = self.session_repo.get_by_id(chat_id)
        if session:
            session.message_count = (session.message_count or 0) + 1
            now = datetime.utcnow()
            session.updated_at = now
            session.last_message_at = now
            if commit:
                self.db.commit()

        return message

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
    ) -> bool:
        """Overwrite the in-flight assistant row while its run streams.

        One statement, conditioned on the run lease, so a superseded worker
        cannot clobber its successor's row and no row lock is held across
        Python work. Nothing else is touched — in particular no session
        timestamps, so pollers do not mistake a checkpoint for a new message.
        Returns False when the caller no longer owns the run.
        """
        extra = dict(extra_data)
        kept_thinking, kept_segments = clamp_thinking(thinking, extra.get("segments"))
        if "segments" in extra:
            extra["segments"] = kept_segments
        owned = exists().where(
            ChatRun.run_id == run_id,
            ChatRun.lease_owner == owner,
            ChatRun.status.in_(("pending", "running")),
        )
        result = self.db.execute(
            update(ChatMessage)
            .where(ChatMessage.message_id == message_id, owned)
            .values(
                content=clamp_message_content(content),
                thinking=_strip_nul(kept_thinking),
                tool_calls=_strip_nul(tool_calls),
                extra_data=_strip_nul(extra),
            )
        )
        return result.rowcount > 0

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
    ) -> ChatMessage:
        """Idempotently upsert a message by message_id: overwrite in place if it exists, otherwise create.

        The autonomous loop uses this for "incrementally refreshing the same assistant message as
        progress advances" — each requirement processed / each evaluation round flushes the
        current accumulated body + tool cards into this message, so progress is visible in the DB
        even after a mid-run crash/refresh, instead of only being written once at the terminal
        state. The update path does not re-increment message_count (only +1 on creation).
        """
        existing = self.message_repo.get_by_id(message_id)
        if existing is not None:
            update: Dict[str, Any] = {"content": clamp_message_content(content)}
            extra = dict(extra_data) if extra_data is not None else None
            kept_thinking, kept_segments = clamp_thinking(
                thinking, (extra or {}).get("segments")
            )
            if model is not None:
                update["model"] = model
            if thinking is not None:
                update["thinking"] = kept_thinking
            if tool_calls is not None:
                update["tool_calls"] = tool_calls
            if usage is not None:
                update["usage"] = usage
            if error is not None:
                update["error"] = error
            if extra is not None:
                if "segments" in extra:
                    extra["segments"] = kept_segments
                update["extra_data"] = extra
            msg = self.message_repo.update(message_id, update, commit=commit)
            session = self.session_repo.get_by_id(chat_id)
            if session:
                now = datetime.utcnow()
                session.updated_at = now
                session.last_message_at = now
                if commit:
                    self.db.commit()
            return msg or existing
        return self.add_message(
            chat_id=chat_id,
            role=role,
            content=content,
            model=model,
            thinking=thinking,
            tool_calls=tool_calls,
            usage=usage,
            error=error,
            extra_data=extra_data,
            message_id=message_id,
            commit=commit,
            chat_seq=chat_seq,
        )

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

    def delete_messages_from(self, chat_id: str, message_id: str) -> int:
        """Delete a message and all subsequent messages in the chat.

        History rewrites invalidate the entire compaction lineage in the same
        transaction.  This fences an in-flight summarizer through the lineage
        version CAS and forces the next replay/compaction to rebuild from raw
        retained messages instead of trusting a stale covered watermark.

        Returns the number of messages deleted.
        """
        self._ensure_message_sequences(chat_id)
        target = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == message_id,
            )
            .first()
        )
        if not target:
            return 0

        self._invalidate_compaction_lineage_for_rewrite(
            chat_id, rewritten_seq=int(target.chat_seq)
        )

        deleted = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.chat_seq >= int(target.chat_seq),
            )
            .delete(synchronize_session="fetch")
        )

        # Update session message count
        session = self.session_repo.get_by_id(chat_id)
        if session:
            remaining = (
                self.db.query(ChatMessage)
                .filter(
                    ChatMessage.chat_id == chat_id,
                )
                .count()
            )
            session.message_count = remaining
            session.updated_at = datetime.utcnow()

        self.db.commit()
        return deleted

    def _invalidate_compaction_lineage_for_rewrite(
        self,
        chat_id: str,
        *,
        rewritten_seq: int,
    ) -> bool:
        """Fence a snapshot that may contain a rewritten/deleted message."""
        state = self._ensure_compaction_state(chat_id)
        # Lock the lineage row where supported. SQLite serializes the following
        # write transaction; PostgreSQL blocks a concurrent publisher here.
        state = (
            self.db.query(ChatCompactionState)
            .filter(ChatCompactionState.chat_id == chat_id)
            .with_for_update()
            .populate_existing()
            .one()
        )
        affects_checkpoint = bool(
            state.active_checkpoint_id
            and int(rewritten_seq) <= int(state.covered_seq or 0)
        )
        affects_snapshot = bool(state.lease_owner)
        if not affects_checkpoint and not affects_snapshot:
            return False

        state.active_checkpoint_id = None
        state.checkpoint_version = int(state.checkpoint_version or 0) + 1
        state.covered_seq = 0
        state.lease_owner = None
        state.lease_expires_at = None
        state.updated_at = _utcnow()

        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        checkpoints = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "system")
            .all()
        )
        for checkpoint in checkpoints:
            extra = dict(checkpoint.extra_data or {})
            if extra.get("kind") != COMPACTION_CHECKPOINT_KIND:
                continue
            extra["active"] = False
            extra["notice_pending"] = False
            checkpoint.extra_data = extra
        return True

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
        """Compatibility wrapper around the lease + CAS checkpoint protocol."""
        owner = f"direct:{uuid.uuid4().hex}"
        snapshot = self.acquire_compaction_snapshot(
            chat_id, owner=owner, lease_seconds=60
        )
        if snapshot is None:
            raise CompactionCASConflict(
                "no uncompacted source or compaction lease is busy"
            )
        if (
            base_checkpoint_version is not None
            and snapshot.base_checkpoint_version != int(base_checkpoint_version)
        ):
            self.release_compaction_lease(chat_id, owner=owner)
            raise CompactionCASConflict("checkpoint base changed")
        if covered_seq is not None and int(covered_seq) != snapshot.covered_seq:
            self.release_compaction_lease(chat_id, owner=owner)
            raise ValueError("covered_seq must equal the acquired source watermark")
        if source_hash:
            snapshot = replace(snapshot, source_hash=source_hash)
        return self.commit_compaction_checkpoint(
            snapshot,
            owner=owner,
            summary_text=summary_text,
            replacement_history=replacement_history,
            replacement_manifest=replacement_manifest or {},
        )

    def _latest_compaction_checkpoint_fallback(
        self, chat_id: str
    ) -> Optional[ChatMessage]:
        """Find the newest checkpoint without timestamps (legacy-state fallback)."""
        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        rows = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "system")
            .order_by(ChatMessage.chat_seq.desc(), ChatMessage.message_id.desc())
            .limit(20)
            .all()
        )
        return next(
            (
                row
                for row in rows
                if (row.extra_data or {}).get("kind") == COMPACTION_CHECKPOINT_KIND
            ),
            None,
        )

    @staticmethod
    def _checkpoint_covered_seq(checkpoint: Optional[ChatMessage]) -> int:
        """Return only an explicitly captured source watermark.

        A legacy checkpoint was inserted *after* summarization, so its own
        sequence cannot prove which earlier rows were actually summarized.
        Treating ``checkpoint.chat_seq - 1`` as covered can hide a message
        that arrived while the summary was in flight.  Legacy checkpoints are
        therefore conservatively invalidated and rebuilt from raw history.
        """
        if checkpoint is None:
            return 0
        extra = checkpoint.extra_data or {}
        explicit = extra.get("covered_seq")
        if explicit is None:
            explicit = extra.get("covers_up_to_chat_seq")
        if explicit is not None:
            return max(0, int(explicit))
        return 0

    def _ensure_message_sequences(self, chat_id: str) -> None:
        """Repair legacy/direct ORM rows before they enter a source snapshot."""
        from sqlalchemy import func

        # ``key_share=True`` renders ``FOR NO KEY UPDATE`` on PostgreSQL.
        # A plain ``FOR UPDATE`` here is a full outage risk: inserting any row
        # into ``chat_messages`` takes ``FOR KEY SHARE`` on this parent row for
        # the foreign key, and ``FOR KEY SHARE`` conflicts with ``FOR UPDATE``.
        # Holding that lock therefore blocks every message write for the chat —
        # including the run journal's terminal commit, which runs on the event
        # loop and freezes the whole process when it waits. We only bump the
        # non-key column ``next_message_seq``, so the weaker lock is both
        # sufficient (it still excludes a concurrent sequence repair) and
        # compatible with the foreign-key lock.
        chat = (
            self.db.query(ChatSession)
            .filter(ChatSession.chat_id == chat_id)
            .with_for_update(key_share=True)
            .one_or_none()
        )
        if chat is None:
            return
        max_seq = int(
            self.db.query(func.max(ChatMessage.chat_seq))
            .filter(ChatMessage.chat_id == chat_id)
            .scalar()
            or 0
        )
        next_seq = max(int(chat.next_message_seq or 1), max_seq + 1)
        missing = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.chat_id == chat_id, ChatMessage.chat_seq.is_(None))
            .order_by(ChatMessage.created_at, ChatMessage.message_id)
            .all()
        )
        for row in missing:
            row.chat_seq = next_seq
            next_seq += 1
        chat.next_message_seq = next_seq
        self.db.flush()

    def _ensure_compaction_state(self, chat_id: str) -> ChatCompactionState:
        state = self.db.get(ChatCompactionState, chat_id)
        if state is not None:
            return state
        checkpoint = self._latest_compaction_checkpoint_fallback(chat_id)
        extra = (checkpoint.extra_data or {}) if checkpoint is not None else {}
        has_exact_watermark = (
            checkpoint is not None
            and (
                extra.get("covered_seq") is not None
                or extra.get("covers_up_to_chat_seq") is not None
            )
        )
        state = ChatCompactionState(
            chat_id=chat_id,
            active_checkpoint_id=checkpoint.message_id if has_exact_watermark else None,
            checkpoint_version=(
                int(extra.get("checkpoint_version") or 1) if has_exact_watermark else 0
            ),
            covered_seq=self._checkpoint_covered_seq(checkpoint)
            if has_exact_watermark
            else 0,
            updated_at=_utcnow(),
        )
        self.db.add(state)
        self.db.flush()
        return state

    def acquire_compaction_snapshot(
        self,
        chat_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> Optional[CompactionSnapshot]:
        """Capture an exact source watermark and acquire a short-lived lease.

        Every exit path must end this transaction. The body takes a row lock on
        the chat before it reads, so an exception escaping with the transaction
        still open strands that lock on a pooled connection that no longer has
        a Python owner — an ``idle in transaction`` backend that blocks the
        chat's message writes until the process dies. The rollback below is the
        guarantee that cannot be forgotten by a future early return.
        """
        try:
            return self._acquire_compaction_snapshot(
                chat_id, owner=owner, lease_seconds=lease_seconds
            )
        except BaseException:
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001 - never mask the original failure
                logger.exception(
                    "[compaction] rollback failed after a snapshot error (chat=%s)",
                    chat_id,
                )
            raise

    def _acquire_compaction_snapshot(
        self,
        chat_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> Optional[CompactionSnapshot]:
        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        self._ensure_message_sequences(chat_id)
        state = self._ensure_compaction_state(chat_id)
        now = _utcnow()
        if state.lease_owner and (_as_utc(state.lease_expires_at) or now) > now:
            self.db.rollback()
            return None

        checkpoint = (
            self.db.get(ChatMessage, state.active_checkpoint_id)
            if state.active_checkpoint_id
            else None
        )
        replacement_history = (
            tuple(
                dict(item)
                for item in (
                    (checkpoint.extra_data or {}).get("replacement_history") or []
                )
            )
            if checkpoint is not None
            else ()
        )
        candidates = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.chat_seq > int(state.covered_seq or 0),
            )
            .order_by(ChatMessage.chat_seq)
            .all()
        )
        source_models = tuple(
            row
            for row in candidates
            if not (
                row.role == "system"
                and (row.extra_data or {}).get("kind") == COMPACTION_CHECKPOINT_KIND
            )
        )
        if not source_models:
            self.db.rollback()
            return None

        source_rows = tuple(
            CompactionSourceRow(
                message_id=row.message_id,
                chat_seq=int(row.chat_seq or 0),
                role=row.role,
                content=row.content,
                tool_calls=row.tool_calls,
                extra_data=dict(row.extra_data or {}),
            )
            for row in source_models
        )
        covered_seq = max(row.chat_seq for row in source_rows)
        canonical_source = [
            {
                "message_id": row.message_id,
                "chat_seq": row.chat_seq,
                "role": row.role,
                "content": row.content,
                "tool_calls": row.tool_calls,
                "metadata": row.extra_data,
            }
            for row in source_rows
        ]
        source_hash = hashlib.sha256(
            json.dumps(
                {
                    "base_checkpoint_version": int(state.checkpoint_version or 0),
                    "base_covered_seq": int(state.covered_seq or 0),
                    "covered_seq": covered_seq,
                    "source": canonical_source,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        base_version = int(state.checkpoint_version or 0)
        updated = (
            self.db.query(ChatCompactionState)
            .filter(
                ChatCompactionState.chat_id == chat_id,
                ChatCompactionState.checkpoint_version == base_version,
                (
                    ChatCompactionState.lease_owner.is_(None)
                    | (ChatCompactionState.lease_expires_at <= now)
                    | (ChatCompactionState.lease_owner == owner)
                ),
            )
            .update(
                {
                    ChatCompactionState.lease_owner: owner,
                    ChatCompactionState.lease_expires_at: now
                    + timedelta(seconds=max(1, lease_seconds)),
                    ChatCompactionState.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            self.db.rollback()
            return None
        base_covered_seq = int(state.covered_seq or 0)
        base_checkpoint_id = checkpoint.message_id if checkpoint is not None else None
        self.db.commit()
        return CompactionSnapshot(
            chat_id=chat_id,
            owner=owner,
            base_checkpoint_id=base_checkpoint_id,
            base_checkpoint_version=base_version,
            base_covered_seq=base_covered_seq,
            covered_seq=covered_seq,
            source_hash=source_hash,
            replacement_history=replacement_history,
            source_rows=source_rows,
            source_message_ids=tuple(row.message_id for row in source_rows),
            source_message_seqs=tuple(row.chat_seq for row in source_rows),
        )

    def release_compaction_lease(self, chat_id: str, *, owner: str) -> bool:
        updated = (
            self.db.query(ChatCompactionState)
            .filter(
                ChatCompactionState.chat_id == chat_id,
                ChatCompactionState.lease_owner == owner,
            )
            .update(
                {
                    ChatCompactionState.lease_owner: None,
                    ChatCompactionState.lease_expires_at: None,
                    ChatCompactionState.updated_at: _utcnow(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return updated == 1

    def commit_compaction_checkpoint(
        self,
        snapshot: CompactionSnapshot,
        *,
        owner: str,
        summary_text: str,
        replacement_history: List[Dict[str, Any]],
        replacement_manifest: Dict[str, Any],
    ) -> ChatMessage:
        """Atomically publish one successor from ``snapshot`` using CAS."""
        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        if owner != snapshot.owner:
            raise CompactionCASConflict("snapshot owner mismatch")
        message_id = f"cmpct_{uuid.uuid4().hex[:16]}"
        next_version = snapshot.base_checkpoint_version + 1
        manifest = dict(replacement_manifest)
        manifest.update(
            {
                "source_message_ids": list(snapshot.source_message_ids),
                "source_message_seqs": list(snapshot.source_message_seqs),
                "source_count": len(snapshot.source_rows),
                "replacement_count": len(replacement_history),
            }
        )
        message = self.message_repo.create(
            {
                "message_id": message_id,
                "chat_id": snapshot.chat_id,
                "role": "system",
                "content": summary_text,
                "extra_data": {
                    "kind": COMPACTION_CHECKPOINT_KIND,
                    "replacement_history": replacement_history,
                    "covered_seq": snapshot.covered_seq,
                    "covers_up_to_chat_seq": snapshot.covered_seq,
                    "covers_up_to_message_id": (
                        snapshot.source_message_ids[-1]
                        if snapshot.source_message_ids
                        else None
                    ),
                    "source_hash": snapshot.source_hash,
                    "base_checkpoint_version": snapshot.base_checkpoint_version,
                    "checkpoint_version": next_version,
                    "replacement_manifest": manifest,
                    "active": True,
                    "notice_pending": True,
                },
            },
            commit=False,
        )
        now = _utcnow()
        updated = (
            self.db.query(ChatCompactionState)
            .filter(
                ChatCompactionState.chat_id == snapshot.chat_id,
                ChatCompactionState.checkpoint_version
                == snapshot.base_checkpoint_version,
                ChatCompactionState.covered_seq == snapshot.base_covered_seq,
                ChatCompactionState.lease_owner == owner,
            )
            .update(
                {
                    ChatCompactionState.active_checkpoint_id: message_id,
                    ChatCompactionState.checkpoint_version: next_version,
                    ChatCompactionState.covered_seq: snapshot.covered_seq,
                    ChatCompactionState.lease_owner: None,
                    ChatCompactionState.lease_expires_at: None,
                    ChatCompactionState.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            self.db.rollback()
            raise CompactionCASConflict("checkpoint base or lease changed")

        if snapshot.base_checkpoint_id:
            previous = self.db.get(ChatMessage, snapshot.base_checkpoint_id)
            if previous is not None:
                previous_extra = dict(previous.extra_data or {})
                previous_extra["active"] = False
                previous.extra_data = previous_extra
        self.db.commit()
        return message

    def get_latest_compaction_checkpoint(self, chat_id: str) -> Optional[ChatMessage]:
        """Return the chat's latest compaction checkpoint (None if there is none).

        The kind filter is done on the Python side to avoid depending on Postgres-specific
        ``->>`` (SQLite compatible). Each checkpoint's extra_data carries the entire
        replacement_history (can reach ~100KB), and rolling compaction keeps accumulating —
        scan only the latest few rows, never ``.all()`` load everything.
        """
        state = self.db.get(ChatCompactionState, chat_id)
        if state is not None:
            if not state.active_checkpoint_id:
                return None
            checkpoint = self.db.get(ChatMessage, state.active_checkpoint_id)
            if (
                checkpoint is not None
                and (checkpoint.extra_data or {}).get("covered_seq") is not None
            ):
                return checkpoint
            return None

        checkpoint = self._latest_compaction_checkpoint_fallback(chat_id)
        if (
            checkpoint is None
            or (checkpoint.extra_data or {}).get("covered_seq") is None
        ):
            return None
        return checkpoint

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

    def update_message_extra_data(
        self,
        message_id: str,
        patch: Dict[str, Any],
    ) -> bool:
        """Merge *patch* into a message's extra_data. Returns True on success."""
        return self.message_repo.update_extra_data(message_id, patch) is not None

    def accept_ontology_revision(self, message_id: str) -> Optional[ChatMessage]:
        """Replace an assistant message with its persisted ontology revision candidate."""
        message = self.message_repo.get_by_id(message_id)
        if not message or message.role != "assistant":
            return None
        extra_data = dict(message.extra_data or {})
        governance = extra_data.get("ontology_governance")
        if not isinstance(governance, dict):
            return None
        review = governance.get("review")
        if not isinstance(review, dict):
            return None
        candidate = normalize_revision_candidate(review.get("candidate_answer"))
        if not is_substantive_revision(candidate):
            return None
        updated_review = {**review, "candidate_answer": candidate, "accepted": True}
        extra_data["ontology_governance"] = {**governance, "review": updated_review}
        # Some pure unit adapters exercise this method without a database.
        if not hasattr(self, "db"):
            return self.message_repo.update(
                message_id,
                {"content": candidate, "extra_data": extra_data},
            )

        updated = self.message_repo.update(
            message_id,
            {"content": candidate, "extra_data": extra_data},
            commit=False,
        )
        self._invalidate_compaction_lineage_for_rewrite(
            message.chat_id,
            rewritten_seq=int(message.chat_seq),
        )
        self.db.commit()
        return updated

    def search_sessions(
        self,
        user_id: str,
        query: str,
        page: int = 1,
        page_size: int = 20,
        scope: str = "title",
    ) -> Tuple[list, int]:
        """Search chat sessions by title (and optionally message content)."""
        results, total = self.session_repo.search(
            user_id, query, page, page_size, scope=scope
        )
        return results, total
