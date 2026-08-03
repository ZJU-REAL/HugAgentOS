"""Reading the memory store itself, which is what makes memory evolution possible.

Every memory operation the design calls for — merge near-duplicates, resolve
contradictions, decay the unretrieved — is an operation on **inventory**. None of
them can be derived from the episode event stream, which only ever says "this
content hash was retrieved". Counting retrievals of one hash and calling the
result a duplicate is not a weak approximation of duplicate detection; it is a
different measurement entirely, and it points the wrong way: a memory retrieved
in five separate conversations is *useful*, and proposing to merge it away
punishes the store's best entries.

So this module scans the store, pairwise, in the embedding space that already
backs retrieval:

* **near-duplicates** — two distinct rows saying the same thing. They cost the
  bounded retrieval budget twice and crowd out everything else.
* **contradictions** — two rows about the same subject pointing opposite ways.
  Retrieval then becomes a coin flip, and the newer one is not automatically
  right, which is why this produces a diagnosis rather than an overwrite.
* **stale** — written long ago, never retrieved. An unbounded store degrades
  precision for everything in it.

Cost is the reason this is a nightly job and not a request-path concern: it is
O(n²) in one user's memories. That is fine at the scale of one person's store
and would not be at fleet scale, so the scan is per-user by construction.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Two memories this close are saying the same thing. Set above the intent-
# clustering threshold on purpose: clustering asks "do these requests mean the
# same?", this asks "is keeping both of these pointless?", and the second is a
# stronger claim that should need stronger evidence.
DUPLICATE_THRESHOLD = 0.93
# Close enough to be about the same subject, and therefore worth checking for a
# contradiction. Below this, two memories that disagree are simply about
# different things.
SAME_SUBJECT_THRESHOLD = 0.72
# Never retrieved in this long → the store is carrying it for nothing.
STALE_AFTER_DAYS = 90
# A store smaller than this cannot crowd anything out, so consolidation buys
# nothing and only risks a wrong merge.
MIN_STORE_SIZE = 8

FINDING_DUPLICATE = "duplicate"
FINDING_CONTRADICTION = "contradiction"
FINDING_STALE = "stale"

# Lexical markers of negation/opposition. Used only to *raise* a pair for review
# once embeddings already say they are about the same subject — a paraphrase and
# its negation sit close together in embedding space, which is precisely why
# similarity alone cannot separate "restates" from "contradicts".
_NEGATION_MARKERS = (
    "不要", "不用", "别", "无需", "不再", "禁止", "取消", "改为", "不是", "并非",
    "never", "not ", "no longer", "instead of", "stop ",
)


@dataclass
class MemoryRecord:
    """One entry as it exists in the store."""

    memory_id: str
    content: str
    content_hash: str
    workspace_id: str = "default"
    # L2 writes procedures only. Entries written before that change carry
    # whatever type they were given, which is why the field is still read
    # rather than assumed — but nothing new is ever written as a fact.
    memory_type: str = "procedural"
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def applies_to(self) -> str:
        return str(self.metadata.get("applies_to") or "")

    @property
    def why(self) -> str:
        return str(self.metadata.get("why") or "")


@dataclass
class ScanFinding:
    """One thing worth doing about the store's contents."""

    kind: str
    refs: List[str]
    memory_ids: List[str]
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "refs": list(self.refs),
            "memory_ids": list(self.memory_ids),
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def to_records(raw_items: Sequence[Dict[str, Any]]) -> List[MemoryRecord]:
    """Normalise the store's rows into something scannable."""
    from core.memory.retrieval_types import content_hash

    records: List[MemoryRecord] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("memory") or raw.get("content") or "").strip()
        if not text:
            continue
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        records.append(
            MemoryRecord(
                memory_id=str(raw.get("id") or raw.get("memory_id") or ""),
                content=text,
                content_hash=content_hash(text),
                workspace_id=str(meta.get("workspace_id") or "default"),
                memory_type=str(meta.get("memory_type") or "procedural"),
                created_at=_parse_time(raw.get("created_at") or raw.get("updated_at")),
                metadata=meta,
            )
        )
    return records


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(texts: List[str]) -> Optional[List[List[float]]]:
    try:
        from core.kb.kb_vector import embed_batch

        vectors = embed_batch(texts)
    except Exception as exc:  # noqa: BLE001
        logger.info("[memory-scan] embeddings unavailable: %s", exc)
        return None
    if not vectors or len(vectors) != len(texts) or not all(vectors):
        return None
    return vectors


def _looks_opposed(a: str, b: str) -> bool:
    """Whether exactly one of the two carries a negation.

    Both negated, or neither, is agreement in whatever direction; one negated is
    the shape of a contradiction. This is a *filter on candidates embeddings
    already grouped*, never a claim on its own — which is why the finding it
    produces is a diagnosis for review rather than an applied change.
    """
    has_a = any(marker in a for marker in _NEGATION_MARKERS)
    has_b = any(marker in b for marker in _NEGATION_MARKERS)
    return has_a != has_b


def scan_records(
    records: Sequence[MemoryRecord],
    *,
    user_id: str,
    retrieved_refs: Optional[Dict[str, int]] = None,
    now: Optional[datetime] = None,
    vectors: Optional[List[List[float]]] = None,
) -> List[ScanFinding]:
    """Findings about one subject's memory inventory.

    ``vectors`` is injectable so this is testable without an embedding service;
    when absent it is computed, and when it cannot be computed the pairwise
    findings are skipped rather than approximated lexically. A lexical
    near-duplicate check would miss paraphrase entirely — the same limitation
    measured on intent clustering — and a merge proposal that fires only on
    near-identical strings would consolidate the cases nobody minds while
    leaving the ones that actually cost budget.
    """
    from core.memory.ref_shadow import build_ref_id
    from core.memory.retrieval_types import LAYER_FACT

    findings: List[ScanFinding] = []
    items = list(records)
    if not items:
        return findings

    now = now or datetime.now(timezone.utc)
    retrieved_refs = retrieved_refs or {}

    def ref_of(record: MemoryRecord) -> str:
        return build_ref_id(
            layer=LAYER_FACT,
            user_id=user_id,
            workspace_id=record.workspace_id,
            content_hash=record.content_hash,
        )

    # ── Stale: written long ago, never retrieved ─────────────────────────────
    cutoff = now - timedelta(days=STALE_AFTER_DAYS)
    for record in items:
        if record.created_at is None or record.created_at > cutoff:
            continue
        ref = ref_of(record)
        if retrieved_refs.get(ref):
            continue
        findings.append(
            ScanFinding(
                kind=FINDING_STALE,
                refs=[ref],
                memory_ids=[record.memory_id],
                reason=(
                    f"写入已超过 {STALE_AFTER_DAYS} 天且从未被检索命中——"
                    "无界增长的记忆库会拉低所有条目的检索精度"
                ),
                detail={
                    "age_days": (now - record.created_at).days,
                    "memory_type": record.memory_type,
                },
            )
        )

    if len(items) < MIN_STORE_SIZE:
        return findings

    if vectors is None:
        vectors = _embed([r.content for r in items])
    if vectors is None or len(vectors) != len(items):
        logger.info("[memory-scan] pairwise scan skipped: no embeddings")
        return findings

    # ── Pairwise: duplicates and contradictions ──────────────────────────────
    merged_into: Dict[int, int] = {}
    for i in range(len(items)):
        if i in merged_into:
            continue
        for j in range(i + 1, len(items)):
            if j in merged_into:
                continue
            if items[i].workspace_id != items[j].workspace_id:
                continue
            similarity = _cosine(vectors[i], vectors[j])
            if similarity < SAME_SUBJECT_THRESHOLD:
                continue

            opposed = _looks_opposed(items[i].content, items[j].content)
            if similarity >= DUPLICATE_THRESHOLD and not opposed:
                merged_into[j] = i
                findings.append(
                    ScanFinding(
                        kind=FINDING_DUPLICATE,
                        refs=[ref_of(items[i]), ref_of(items[j])],
                        memory_ids=[items[i].memory_id, items[j].memory_id],
                        reason=(
                            f"两条记忆语义重复（相似度 {similarity:.2f}）——"
                            "重复条目在有界检索预算里会挤掉其它记忆"
                        ),
                        detail={
                            "similarity": round(similarity, 4),
                            "keep": items[i].content[:160],
                            "supersede": items[j].content[:160],
                        },
                    )
                )
            elif opposed:
                findings.append(
                    ScanFinding(
                        kind=FINDING_CONTRADICTION,
                        refs=[ref_of(items[i]), ref_of(items[j])],
                        memory_ids=[items[i].memory_id, items[j].memory_id],
                        reason=(
                            f"两条记忆指向同一主体但方向相反（相似度 {similarity:.2f}）——"
                            "检索到哪一条会变成抛硬币，且新的那条不自动就是对的"
                        ),
                        detail={
                            "similarity": round(similarity, 4),
                            "a": items[i].content[:160],
                            "b": items[j].content[:160],
                            "a_created": items[i].created_at.isoformat()
                            if items[i].created_at
                            else "",
                            "b_created": items[j].created_at.isoformat()
                            if items[j].created_at
                            else "",
                        },
                    )
                )

    return findings


async def scan_user_memories(
    user_id: str,
    *,
    workspace_id: Optional[str] = None,
    retrieved_refs: Optional[Dict[str, int]] = None,
    limit: int = 500,
) -> Tuple[List[MemoryRecord], List[ScanFinding]]:
    """Load one subject's store and scan it."""
    from core.memory.service import get_all_memories

    raw = await get_all_memories(user_id, workspace_id=workspace_id, top_k=limit)
    records = to_records(raw)
    return records, scan_records(records, user_id=user_id, retrieved_refs=retrieved_refs)


def procedural_records(records: Sequence[MemoryRecord]) -> List[MemoryRecord]:
    """Only the entries a skill could legitimately be compiled from."""
    return [r for r in records if r.memory_type == "procedural"]
