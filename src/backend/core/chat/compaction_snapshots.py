"""Capture the message snapshot a compaction summarises and hold its writer lease."""
from __future__ import annotations
import hashlib
import json
from datetime import timedelta
from typing import Optional
from core.db.models import ChatCompactionState, ChatMessage, ChatSession
from core.chat import compaction_types
import logging
from core.infra.time import as_utc, utc_now
logger = logging.getLogger(__name__)


def compaction_lease_held(state) -> bool:
    """Whether another compactor holds the chat's compaction lease right now."""
    if state is None or not state.lease_owner:
        return False
    now = utc_now()
    return (as_utc(state.lease_expires_at) or now) > now

def ensure_message_sequences(self, chat_id: str) -> None:
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


def ensure_compaction_state(self, chat_id: str) -> ChatCompactionState:
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
        updated_at=utc_now(),
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
) -> Optional[compaction_types.CompactionSnapshot]:
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


def acquire_compaction_snapshot_impl(
    self,
    chat_id: str,
    *,
    owner: str,
    lease_seconds: int,
) -> Optional[compaction_types.CompactionSnapshot]:
    from core.llm.context.compaction import COMPACTION_CHECKPOINT_KIND

    self._ensure_message_sequences(chat_id)
    state = self._ensure_compaction_state(chat_id)
    if compaction_lease_held(state):
        self.db.rollback()
        return None
    now = utc_now()

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
        compaction_types.CompactionSourceRow(
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
    return compaction_types.CompactionSnapshot(
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
                ChatCompactionState.updated_at: utc_now(),
            },
            synchronize_session=False,
        )
    )
    self.db.commit()
    return updated == 1
