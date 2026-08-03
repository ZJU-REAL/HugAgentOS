"""Retrieval-time weighting applied on top of what the store returns.

Memory evolution has to be able to say "surface this one less" and "this one has
been superseded" — and then be able to take it back. Neither is expressible
against the vector store directly: mem0 owns the row, renumbers it on merge, and
offers no notion of a weight the retriever should honour. Writing the change
into the store would also make it irreversible in practice, because by the time
someone wants it undone the row it applied to may no longer exist under that id.

So the change lives here instead, as an **overlay keyed by the content-derived
ref** (:func:`core.memory.ref_shadow.build_ref_id`), which survives the store
renumbering itself. Three consequences, all of them the point:

* **Reversible by construction.** Undo is deleting a row, not reconstructing a
  previous state inside someone else's store.
* **Never destructive.** The strongest thing an overlay can do is stop a memory
  from being injected. The memory itself is untouched, so a wrong decision
  costs recall, not data — and from the user's side, a system that silently
  deleted their memory is indistinguishable from one that lost it.
* **Auditable.** Every entry traces to the candidate that proposed it, so
  "why did this stop showing up?" has an answer.

The read path is a single indexed query per retrieval, cached briefly, and
degrades to "no overlay" — a weighting mechanism must never be able to fail a
turn.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OP_REWEIGHT = "reweight"
OP_SUPERSEDE = "supersede"

STATUS_APPLIED = "applied"
STATUS_REVERTED = "reverted"

# Below this the memory is effectively withheld. Kept as a floor rather than
# allowing zero so "suppressed" stays a separate, explicit operation: a weight
# is a ranking statement, suppression is a visibility one, and collapsing them
# would make an aggressive reweight silently equivalent to a delete.
MIN_WEIGHT = 0.05

_CACHE_TTL_S = 30.0
_cache: Dict[str, Tuple[float, Dict[str, float], set]] = {}


def _cache_key(user_id: str, workspace_id: str) -> str:
    return f"{user_id}|{workspace_id or 'default'}"


def invalidate(user_id: str = "", workspace_id: str = "") -> None:
    """Drop cached overlays so an applied change takes effect on the next turn."""
    if not user_id:
        _cache.clear()
        return
    _cache.pop(_cache_key(user_id, workspace_id), None)


def load_overlay(user_id: str, workspace_id: str = "default") -> Tuple[Dict[str, float], set]:
    """``(weights_by_ref, superseded_refs)`` for this subject."""
    if not user_id:
        return {}, set()

    key = _cache_key(user_id, workspace_id)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_S:
        return cached[1], cached[2]

    weights: Dict[str, float] = {}
    superseded: set = set()
    try:
        from core.db.engine import SessionLocal
        from core.db.models import EvolutionMemoryOp

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionMemoryOp)
                .filter(
                    EvolutionMemoryOp.user_id == user_id,
                    EvolutionMemoryOp.status == STATUS_APPLIED,
                )
                .order_by(EvolutionMemoryOp.applied_at.asc())
                .all()
            )
        for row in rows:
            if workspace_id and row.workspace_id and row.workspace_id != workspace_id:
                continue
            ref = str(row.memory_ref or "")
            if not ref:
                continue
            if row.operation == OP_SUPERSEDE:
                superseded.add(ref)
            elif row.operation == OP_REWEIGHT:
                after = row.after_state or {}
                try:
                    weights[ref] = max(MIN_WEIGHT, float(after.get("weight", 1.0)))
                except (TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("[memory-weights] overlay unavailable, ignoring: %s", exc)
        return {}, set()

    _cache[key] = (now, weights, superseded)
    return weights, superseded


def apply_overlay(
    items,
    *,
    user_id: str,
    workspace_id: str = "default",
    score_key: str = "_adjusted_score",
) -> list:
    """Reweight and drop, in place of the raw ranking.

    ``items`` are the store's raw dicts, already scored. Returns a new list with
    superseded entries removed and weights applied, re-sorted.
    """
    if not items or not user_id:
        return list(items or [])

    weights, superseded = load_overlay(user_id, workspace_id)
    if not weights and not superseded:
        return list(items)

    from core.memory.ref_shadow import build_ref_id
    from core.memory.retrieval_types import LAYER_FACT, content_hash

    kept = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        item_ws = str(meta.get("workspace_id") or workspace_id or "default")
        ref = build_ref_id(
            layer=LAYER_FACT,
            user_id=user_id,
            workspace_id=item_ws,
            content_hash=content_hash(item.get("memory") or ""),
        )
        if ref in superseded:
            continue
        weight = weights.get(ref)
        if weight is not None:
            item[score_key] = float(item.get(score_key, 0.0)) * weight
            item["_evolution_weight"] = weight
        kept.append(item)

    kept.sort(key=lambda x: x.get(score_key, 0.0), reverse=True)
    return kept

