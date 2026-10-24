"""Publish and read compaction checkpoints with lease and compare-and-swap fencing."""
from __future__ import annotations
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Optional
from core.db.models import ChatCompactionState, ChatMessage
from core.chat import compaction_types
from core.infra.time import utc_now

def invalidate_compaction_lineage_for_rewrite(
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
    state.updated_at = utc_now()

    from core.llm.context.compaction import COMPACTION_CHECKPOINT_KIND

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
        raise compaction_types.CompactionCASConflict(
            "no uncompacted source or compaction lease is busy"
        )
    if (
        base_checkpoint_version is not None
        and snapshot.base_checkpoint_version != int(base_checkpoint_version)
    ):
        self.release_compaction_lease(chat_id, owner=owner)
        raise compaction_types.CompactionCASConflict("checkpoint base changed")
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


def latest_compaction_checkpoint_fallback(
    self, chat_id: str
) -> Optional[ChatMessage]:
    """Find the newest checkpoint without timestamps (legacy-state fallback)."""
    from core.llm.context.compaction import COMPACTION_CHECKPOINT_KIND

    from sqlalchemy.orm import defer

    rows = (
        self.db.query(ChatMessage).options(defer(ChatMessage.model_steps), defer(ChatMessage.tool_calls))
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


def checkpoint_covered_seq(checkpoint: Optional[ChatMessage]) -> int:
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


def commit_compaction_checkpoint(
    self,
    snapshot: compaction_types.CompactionSnapshot,
    *,
    owner: str,
    summary_text: str,
    replacement_history: List[Dict[str, Any]],
    replacement_manifest: Dict[str, Any],
) -> ChatMessage:
    """Atomically publish one successor from ``snapshot`` using CAS."""
    from core.llm.context.compaction import COMPACTION_CHECKPOINT_KIND

    if owner != snapshot.owner:
        raise compaction_types.CompactionCASConflict("snapshot owner mismatch")
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
    now = utc_now()
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
        raise compaction_types.CompactionCASConflict("checkpoint base or lease changed")

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
        from sqlalchemy.orm import defer
        checkpoint = self.db.get(ChatMessage, state.active_checkpoint_id,
            options=[defer(ChatMessage.model_steps), defer(ChatMessage.tool_calls)])
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
