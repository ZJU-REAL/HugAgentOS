"""Structured memory-retrieval contract (GCE ticket 01).

Before this module, ``retrieve_memories()`` returned a pre-formatted string and
every piece of identity — ``memory_id``, layer, similarity score, rank — was
discarded at the point of formatting.  That made it impossible to answer "which
memories did this run actually read, and did any of them help?", which the whole
memory-attribution chain depends on.

The contract here keeps the identity all the way up to the orchestration layer;
text assembly becomes a *projection* of the structured result (``to_text()``)
rather than the only available output.  ``to_text()`` reproduces the historical
formatting byte for byte so injection behaviour does not regress.

Layers follow the project's memory tiering:
``profile`` (L1, Postgres) / ``fact`` (L2, vector store) / ``graph`` (L3, graph store).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# Header lines are part of the injection contract — the orchestration layer
# strips the first one when merging into the frozen block, so they must not
# drift without updating memory_integration.
_FACT_HEADER = "## 关于该用户的已知背景信息（来自历史会话记忆）"
_RELATION_HEADER = "\n## 用户相关实体关系"

LAYER_PROFILE = "profile"
LAYER_FACT = "fact"
LAYER_GRAPH = "graph"


def content_hash(text: str) -> str:
    """Stable short hash of a memory's content.

    Used as the join key for the ref-shadow table: external stores rewrite
    their own ids when memories are merged or updated, so content — not the
    vendor id — is what lets us recover "which memory was this" after the fact.
    Whitespace is normalised so a reformatted-but-identical memory keeps its
    identity.
    """
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class RetrievedMemory:
    """One memory item that was recalled and survived filtering."""

    memory_id: str
    layer: str
    content: str
    content_hash: str
    score: float
    adjusted_score: float
    rank: int
    workspace_id: str = "default"
    confidentiality: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_event_payload(self) -> Dict[str, Any]:
        """Compact form for trace events — never carries the raw content.

        Episodes store references, not memory text: the content itself already
        lives in the memory store and duplicating it into the evidence chain
        would widen the privacy surface for no benefit.
        """
        return {
            "memory_id": self.memory_id,
            "layer": self.layer,
            "content_hash": self.content_hash,
            "score": round(self.score, 4),
            "adjusted_score": round(self.adjusted_score, 4),
            "rank": self.rank,
            "workspace_id": self.workspace_id,
            "confidentiality": self.confidentiality,
        }


@dataclass(frozen=True)
class RetrievedRelation:
    """One graph-memory relation triple (L3).

    ``relation_id`` is the stable id the graph store assigned at write time
    (``grel_*``). It is what lets an Episode's L3 evidence be resolved back to
    the exact Neo4j edge weeks later — content alone cannot do that once the
    entities have been renamed or merged. Old traces without it degrade to the
    content hash, so the field is optional rather than required.
    """

    source: str
    relationship: str
    target: str
    rank: int
    workspace_id: str = "default"
    relation_id: str = ""
    predicate: str = ""
    confidence: float = 0.0

    @property
    def is_complete(self) -> bool:
        return bool(self.source and self.relationship and self.target)

    @property
    def content_hash(self) -> str:
        return content_hash(f"{self.source}|{self.relationship}|{self.target}")

    def to_event_payload(self) -> Dict[str, Any]:
        return {
            "layer": LAYER_GRAPH,
            "relation_id": self.relation_id,
            "predicate": self.predicate,
            "confidence": round(self.confidence, 4),
            "content_hash": self.content_hash,
            "rank": self.rank,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True)
class MemoryRetrievalResult:
    """Outcome of one retrieval attempt, including the degraded cases.

    A degraded result is *not* an error: retrieval runs on a hard latency
    budget and silently yields nothing when the budget is blown or the store is
    unreachable.  Recording *why* it was empty is what lets attribution later
    distinguish "memory had nothing to offer" from "memory never got a chance",
    which are very different explanations for the same failure.
    """

    items: Tuple[RetrievedMemory, ...] = ()
    relations: Tuple[RetrievedRelation, ...] = ()
    recalled_count: int = 0
    filtered_count: int = 0
    degraded: bool = False
    degrade_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.items and not self.relations

    def to_text(self) -> str:
        """Render the injectable block.

        Reproduces the pre-ticket-01 formatting exactly, including the edge case
        where every item has blank content and only the header survives.
        """
        if self.is_empty:
            return ""

        lines = [_FACT_HEADER]
        for item in self.items:
            text = (item.content or "").strip()
            if text:
                lines.append(f"- {text}")

        if self.relations:
            lines.append(_RELATION_HEADER)
            for relation in self.relations[:5]:
                if relation.is_complete:
                    lines.append(
                        f"- {relation.source} → {relation.relationship} → {relation.target}"
                    )

        return "\n".join(lines)

    def to_event_payload(self) -> Dict[str, Any]:
        """Summary written into the Episode trace event."""
        return {
            "recalled": self.recalled_count,
            "injected": len(self.items),
            "relations": len(self.relations),
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "items": [item.to_event_payload() for item in self.items],
            "graph": [relation.to_event_payload() for relation in self.relations],
        }

    @classmethod
    def degraded_result(cls, reason: str) -> "MemoryRetrievalResult":
        return cls(degraded=True, degrade_reason=reason)


def build_memory_item(
    raw: Dict[str, Any],
    *,
    rank: int,
    layer: str = LAYER_FACT,
    default_workspace: str = "default",
) -> Optional[RetrievedMemory]:
    """Convert one raw store record into a typed item.

    Returns ``None`` for records the store handed back in an unexpected shape —
    a malformed record must never take down retrieval, which sits on the user's
    critical path.
    """
    if not isinstance(raw, dict):
        return None

    text = raw.get("memory") or ""
    meta = raw.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    try:
        score = float(raw.get("score", 1.0))
    except (TypeError, ValueError):
        score = 0.0
    try:
        adjusted = float(raw.get("_adjusted_score", score))
    except (TypeError, ValueError):
        adjusted = score

    return RetrievedMemory(
        memory_id=str(raw.get("id") or raw.get("memory_id") or ""),
        layer=layer,
        content=text,
        content_hash=content_hash(text),
        score=score,
        adjusted_score=adjusted,
        rank=rank,
        workspace_id=str(meta.get("workspace_id") or default_workspace),
        confidentiality=str(meta.get("confidentiality") or ""),
        metadata=meta,
    )
