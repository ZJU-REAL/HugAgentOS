"""Reorganisation proposals for the memory layer."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence
from core.evolution.cycle import cycle_extras_constants

def memory_findings(
    views: Sequence[Dict[str, Any]],
    *,
    scan_findings: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Reorganisation proposals for the memory layer.

    Two independent sources, because they answer different questions and only
    one of them is answerable from a run's evidence:

    * **inventory** (``scan_findings``) — duplicates, contradictions and stale
      entries, which require reading the store;
    * **behaviour** (``views``) — memories that keep being injected into turns
      that fail, which requires reading the runs.

    The behavioural signal is deliberately *not* framed as duplication. A
    memory injected repeatedly is either helping or not, and the difference is
    the outcome of the turns it landed in — never the count on its own.
    """
    from core.evolution.generators.memory_ops import OP_MERGE, OP_REWEIGHT, SCOPE_USER, MemoryOp, partition_by_approval
    from core.evolution.generators.memory_scan import FINDING_CONTRADICTION, FINDING_DUPLICATE, FINDING_STALE

    retrieved: Dict[str, int] = {}
    helped: Dict[str, int] = {}
    for view in views:
        for ref in view.get("memory_refs") or []:
            key = str(ref)
            retrieved[key] = retrieved.get(key, 0) + 1
            if view.get("verdict") == "success":
                helped[key] = helped.get(key, 0) + 1

    ops: List[MemoryOp] = []
    contradictions: List[Dict[str, Any]] = []

    # 1. Inventory findings, from an actual scan of the store.
    for finding in scan_findings or []:
        payload = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
        kind = payload.get("kind")
        refs = [str(r) for r in (payload.get("refs") or [])]
        if kind == FINDING_DUPLICATE and len(refs) >= 2:
            # The second entry loses. Content is not rewritten and nothing is
            # removed — the survivor already says the same thing.
            ops.append(
                MemoryOp(
                    operation=OP_MERGE,
                    memory_ref=refs[1],
                    scope=SCOPE_USER,
                    reason=payload.get("reason", ""),
                    before={"injected": True, "duplicate_of": refs[0]},
                    after={"injected": False, "superseded_by": refs[0]},
                )
            )
        elif kind == FINDING_STALE and refs:
            ops.append(
                MemoryOp(
                    operation=OP_REWEIGHT,
                    memory_ref=refs[0],
                    scope=SCOPE_USER,
                    reason=payload.get("reason", ""),
                    before={"weight": 1.0},
                    after={"weight": 0.7},
                )
            )
        elif kind == FINDING_CONTRADICTION:
            # Neither side is automatically right, and picking by recency is
            # how a corrected mistake becomes the new truth. This is reported
            # for a human, with both sides and their dates.
            contradictions.append(payload)

    # 2. Behavioural: injected often, associated with failure. Down-weighted,
    #    never deleted — a wrong reweight is recoverable and a wrong deletion is
    #    not.
    for ref, count in retrieved.items():
        if count < cycle_extras_constants.DEMOTE_MIN_HITS:
            continue
        rate = helped.get(ref, 0) / count
        if rate <= cycle_extras_constants.DEMOTE_SUCCESS_CEILING:
            ops.append(
                MemoryOp(
                    operation=OP_REWEIGHT,
                    memory_ref=ref,
                    scope=SCOPE_USER,
                    reason=(
                        f"被注入 {count} 次，其中仅 {helped.get(ref, 0)} 次所在回答成功"
                        "——检索到它并没有帮上忙"
                    ),
                    before={"weight": 1.0},
                    after={"weight": 0.5},
                )
            )

    auto, needs_review = partition_by_approval(ops)
    return {
        "distinct_memories_retrieved": len(retrieved),
        "merge_proposals": sum(1 for op in ops if op.operation == OP_MERGE),
        "reweight_proposals": sum(1 for op in ops if op.operation == OP_REWEIGHT),
        "contradictions": contradictions,
        # Reported separately because the split *is* the governance boundary:
        # reweighting is reversible and may proceed, merging changes what gets
        # injected and may not.
        "auto_applicable": [op.to_dict() for op in auto],
        "needs_review": [op.to_dict() for op in needs_review],
        "ops": ops,
    }
