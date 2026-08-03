"""Per-turn evolution settlement.

The in-chat card answers one question: **what did this turn teach the system?**

The only honest answer at turn scope is *memory*. Skills and orchestration
policies are mined in batch — a turn cannot create one, and the skills it opened
were already installed, so reporting them announced an evolution that never
happened. Every turn that used a skill claimed one, which is how the card
learned to be ignored. So the card reports memory writes and nothing else.

Memory is also the one mechanism a user can act on, which is why the summary
carries the **entries themselves** — text plus the handle needed to edit or
delete each one — rather than a count. A card that says "wrote 3 memories" is
unverifiable and unfixable; one that shows the three sentences and lets the user
correct a wrong one is neither.

Settlement cannot run while the stream is open: the write pipeline is scheduled
after it closes. So the closing frame carries only a marker, settlement happens
in the background (`settlement_runner`), and the result is persisted onto the
message so a reload — or a client that had disconnected — still sees it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── The one mechanism a turn can report ──────────────────────────────────────

MECH_MEMORY = "memory"

# ── Status vocabulary (single source of truth; the client only maps to text) ──

ST_WRITTEN = "written"
ST_FAILED = "failed"

_ALLOWED_STATUSES = {MECH_MEMORY: {ST_WRITTEN, ST_FAILED}}

# Settlement lifecycle
SETTLE_PENDING = "pending"
SETTLE_SETTLED = "settled"
SETTLE_FAILED = "failed"
SETTLE_EMPTY = "empty"

# Layers a written memory can belong to.
LAYER_PROFILE = "L1"
LAYER_PROCEDURE = "L2"


@dataclass
class MemoryEntry:
    """One memory written this turn, as shown on the card.

    ``handle`` is what makes the entry actionable: a mem0 id for an L2 procedure,
    a profile field key for an L1 entry. An entry without one cannot be edited or
    deleted, so it is never emitted.
    """

    layer: str
    handle: str
    text: str
    kind: str = ""
    why: str = ""
    applies_to: str = ""
    action: str = "write"

    def is_valid(self) -> bool:
        return bool(self.handle and self.text and self.layer)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "handle": self.handle,
            "text": self.text,
            "kind": self.kind,
            "why": self.why,
            "applies_to": self.applies_to,
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MemoryEntry":
        return cls(
            layer=str(raw.get("layer") or ""),
            handle=str(raw.get("handle") or ""),
            text=str(raw.get("text") or ""),
            kind=str(raw.get("kind") or ""),
            why=str(raw.get("why") or ""),
            applies_to=str(raw.get("applies_to") or ""),
            action=str(raw.get("action") or "write"),
        )


@dataclass
class EvolutionSummary:
    """What the card and the side panel render for one turn."""

    episode_id: str = ""
    message_id: str = ""
    state: str = SETTLE_PENDING
    status: str = ""
    entries: List[MemoryEntry] = field(default_factory=list)
    error: str = ""
    settled_at: str = ""

    @property
    def gain(self) -> int:
        """Capability delta headline — one per memory actually written."""
        return len(self.entries)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "message_id": self.message_id,
            "state": self.state,
            "status": self.status,
            "gain": self.gain,
            "entries": [e.to_dict() for e in self.entries],
            "error": self.error,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EvolutionSummary":
        return cls(
            episode_id=str(raw.get("episode_id") or ""),
            message_id=str(raw.get("message_id") or ""),
            state=str(raw.get("state") or SETTLE_PENDING),
            status=str(raw.get("status") or ""),
            entries=[MemoryEntry.from_dict(e) for e in (raw.get("entries") or [])],
            error=str(raw.get("error") or ""),
            settled_at=str(raw.get("settled_at") or ""),
        )


def pending_payload(*, message_id: str) -> Dict[str, Any]:
    """The marker carried on the closing frame, so the client knows what to watch.

    It renders nothing. The card appears only once settlement has something to
    show — announcing an evolution before knowing whether one happened is what
    put a spinner at the end of every conversation.
    """
    return {"message_id": message_id, "state": SETTLE_PENDING}


def settle_turn(
    *,
    message_id: str,
    episode_id: str = "",
    memory_entries: Optional[List[Dict[str, Any]]] = None,
    memory_failed: bool = False,
    memory_enabled: bool = True,
) -> EvolutionSummary:
    """Turn the memory pipeline's report into the card payload.

    ``state`` is ``empty`` when nothing was written — the overwhelmingly common
    case — and the client then renders no card at all.
    """
    now = datetime.now(timezone.utc).isoformat()

    if memory_failed:
        return EvolutionSummary(
            episode_id=episode_id,
            message_id=message_id,
            state=SETTLE_SETTLED,
            status=ST_FAILED,
            settled_at=now,
        )

    entries: List[MemoryEntry] = []
    if memory_enabled:
        for raw in memory_entries or []:
            entry = MemoryEntry.from_dict(raw)
            if entry.is_valid():
                entries.append(entry)

    if not entries:
        return EvolutionSummary(
            episode_id=episode_id,
            message_id=message_id,
            state=SETTLE_EMPTY,
            settled_at=now,
        )

    return EvolutionSummary(
        episode_id=episode_id,
        message_id=message_id,
        state=SETTLE_SETTLED,
        status=ST_WRITTEN,
        entries=entries,
        settled_at=now,
    )


def failed_summary(*, message_id: str, episode_id: str = "", error: str) -> EvolutionSummary:
    """Explicit failure state, so the card stops at an error rather than spinning forever."""
    return EvolutionSummary(
        episode_id=episode_id,
        message_id=message_id,
        state=SETTLE_FAILED,
        error=error,
        settled_at=datetime.now(timezone.utc).isoformat(),
    )
