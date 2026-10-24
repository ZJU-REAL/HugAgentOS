"""Snapshot and conflict types shared by the compaction checkpoint protocol."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional

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
