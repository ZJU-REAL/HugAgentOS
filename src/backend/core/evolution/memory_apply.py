"""Making an approved memory change actually happen — and undoable.

Everything upstream of this file produced *proposals*. Nothing applied them, so
approving a memory candidate changed exactly nothing: the reweight that the
governance split called "safe to apply automatically" had no code path that
could apply it. A queue whose approvals do nothing is worse than no queue, since
it reports progress that did not occur.

Three properties, in the order they matter:

* **Nothing is deleted, ever.** The strongest operation here is
  ``supersede``, which stops a memory from being injected. The row stays.
  A wrong reweight costs recall; a wrong deletion costs the user their data, and
  from their side that is indistinguishable from the system having lost it.
* **The undo is written before the change takes effect.** Every applied op
  records its previous state inline, so reverting never has to reconstruct what
  a store looked like weeks ago — by then the vendor may have merged or
  renumbered the row, and an undo that cannot find its target is not an undo.
* **Application is idempotent per (candidate, ref, operation).** Re-running a
  cycle, retrying a request, or approving twice must not stack weights: 0.5
  applied twice is 0.25, which is a different decision than the one approved.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.evolution.memory_ops import (
    OP_MERGE,
    OP_REWEIGHT,
    MemoryOp,
)
from core.memory.weights import OP_SUPERSEDE, STATUS_APPLIED, STATUS_REVERTED

logger = logging.getLogger(__name__)

# What a merge does to the entry that loses. Content is never rewritten and
# nothing is removed: the surviving entry already says the same thing, so the
# duplicate simply stops being injected.
MERGE_ACTION = OP_SUPERSEDE

# Applied when a skill has been compiled out of a memory. Not zero: the memory
# is still true and still the reason the skill exists, it has merely acquired a
# more efficient carrier. Rolling the skill back restores the weight exactly.
PROMOTION_WEIGHT = 0.6


def _op_id(candidate_id: str, ref: str, operation: str) -> str:
    """Deterministic, so the same op applied twice is the same row."""
    seed = f"{candidate_id}|{ref}|{operation}"
    return "mop_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def apply_memory_ops(
    ops: Sequence[MemoryOp],
    *,
    candidate_id: str,
    user_id: str,
    workspace_id: str = "default",
    approver: str = "",
) -> Dict[str, Any]:
    """Apply approved operations to the retrieval overlay.

    Returns a report naming what was applied and what was refused. Refusals are
    reported rather than raised: one unapplicable op in a batch must not discard
    the rest, and an operator needs to see which ones landed.
    """
    from core.db.engine import SessionLocal
    from core.db.models import EvolutionMemoryOp
    from core.memory.weights import invalidate

    applied: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    with SessionLocal() as db:
        for op in ops:
            operation = MERGE_ACTION if op.operation == OP_MERGE else op.operation
            if operation not in (OP_REWEIGHT, OP_SUPERSEDE):
                # An operation with no executor must not report success. The
                # whitelist here is narrower than the IR's on purpose: the IR
                # says what may be *proposed*, this says what can be *done*.
                refused.append(
                    {"memory_ref": op.memory_ref, "operation": op.operation,
                     "reason": "no_executor_for_operation"}
                )
                continue
            if not op.memory_ref:
                refused.append(
                    {"memory_ref": "", "operation": operation, "reason": "missing_ref"}
                )
                continue

            op_id = _op_id(candidate_id, op.memory_ref, operation)
            existing = db.get(EvolutionMemoryOp, op_id)
            if existing is not None and existing.status == STATUS_APPLIED:
                # Already in force. Reporting it as applied keeps retries
                # honest without applying the weight a second time.
                applied.append({"op_id": op_id, "memory_ref": op.memory_ref,
                                "operation": operation, "repeat": True})
                continue

            after = dict(op.after or {})
            before = dict(op.before or {})
            if operation == OP_REWEIGHT and "weight" not in after:
                refused.append(
                    {"memory_ref": op.memory_ref, "operation": operation,
                     "reason": "reweight_without_target_weight"}
                )
                continue

            row = existing or EvolutionMemoryOp(op_id=op_id)
            row.candidate_id = candidate_id
            row.user_id = user_id
            row.workspace_id = workspace_id or "default"
            row.operation = operation
            row.memory_ref = op.memory_ref
            row.before_state = before or {"weight": 1.0}
            row.after_state = after or {"suppressed": True}
            row.reason = (op.reason or "")[:2000]
            row.status = STATUS_APPLIED
            row.applied_at = datetime.now(timezone.utc)
            row.reverted_at = None
            if existing is None:
                db.add(row)
            applied.append(
                {"op_id": op_id, "memory_ref": op.memory_ref, "operation": operation}
            )
        db.commit()

    invalidate(user_id, workspace_id)
    logger.info(
        "[memory-apply] candidate=%s user=%s applied=%d refused=%d approver=%s",
        candidate_id, user_id, len(applied), len(refused), approver,
    )
    return {"applied": applied, "refused": refused}


def revert_memory_ops(
    *, candidate_id: str = "", op_ids: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Undo applied operations.

    Either every op of one candidate, or a named set. Reverting is deleting the
    overlay's claim, not writing a compensating one — a compensating weight
    would leave the store in a third state that matches neither before nor after.
    """
    from core.db.engine import SessionLocal
    from core.db.models import EvolutionMemoryOp
    from core.memory.weights import invalidate

    if not candidate_id and not op_ids:
        return {"reverted": [], "reason": "nothing_specified"}

    reverted: List[str] = []
    touched: List[tuple] = []
    with SessionLocal() as db:
        query = db.query(EvolutionMemoryOp).filter(
            EvolutionMemoryOp.status == STATUS_APPLIED
        )
        if candidate_id:
            query = query.filter(EvolutionMemoryOp.candidate_id == candidate_id)
        if op_ids:
            query = query.filter(EvolutionMemoryOp.op_id.in_(list(op_ids)))
        for row in query.all():
            row.status = STATUS_REVERTED
            row.reverted_at = datetime.now(timezone.utc)
            reverted.append(row.op_id)
            touched.append((row.user_id, row.workspace_id))
        db.commit()

    for user_id, workspace_id in set(touched):
        invalidate(user_id or "", workspace_id or "default")
    logger.info(
        "[memory-apply] reverted %d ops (candidate=%s)", len(reverted), candidate_id
    )
    return {"reverted": reverted}


def demote_promoted_memories(
    *,
    candidate_id: str,
    user_id: str,
    memory_refs: Sequence[str],
    skill_id: str,
    workspace_id: str = "default",
) -> Dict[str, Any]:
    """Down-weight the memories a skill was compiled from.

    This is the reverse edge of the promotion chain, and until now it existed
    only as an uncalled function. Without it the same knowledge is paid for
    twice — once through retrieval, once through the skill — and the retrieval
    half keeps consuming the budget that made compiling it worthwhile.

    Down-weighted, never removed, and tied to the candidate so that rolling the
    skill back restores the memories in the same motion.
    """
    refs = [str(ref) for ref in memory_refs if ref]
    if not refs:
        return {"applied": [], "refused": []}

    ops = [
        MemoryOp(
            operation=OP_REWEIGHT,
            memory_ref=ref,
            reason=f"已编译进技能 {skill_id}；知识有了更省的载体，但仍保留可回滚",
            before={"weight": 1.0},
            after={"weight": PROMOTION_WEIGHT, "promoted_into": skill_id},
        )
        for ref in refs
    ]
    return apply_memory_ops(
        ops,
        candidate_id=candidate_id,
        user_id=user_id,
        workspace_id=workspace_id,
        approver="promotion_chain",
    )




def apply_memory_candidate(
    candidate_id: str,
    *,
    approver: str,
    require_user_id: str = "",
) -> Dict[str, Any]:
    """Apply an approved memory reorganisation candidate.

    Lives here rather than in the admin route because **both** control planes
    need it: the enterprise console approves on behalf of a tenant, and a user
    approves their own memories from the settings panel — which is a Community
    Edition surface. While this was route-local, the user path fell through to
    the skill materialiser and every memory candidate answered
    "暂不支持物化 memory 候选", i.e. every row in that queue was a button that
    could only fail.

    ``require_user_id`` guards the personal path: a user may only apply
    operations against their own memories.
    """
    from core.db.engine import SessionLocal
    from core.db.models.evolution import EvolutionCandidate
    from core.evolution.activation import ActivationError

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if approver == (candidate.proposer or ""):
            raise ActivationError("self_approval_forbidden", "生成者不能批准自己提出的候选")

        change = ((candidate.ir or {}).get("changes") or [{}])[0]
        user_id = str(change.get("user_id") or "") or require_user_id
        raw_ops = change.get("operations") or []
        if not user_id or not raw_ops:
            raise ActivationError("no_memory_payload", "候选未携带可执行的记忆操作")
        if require_user_id and user_id != require_user_id:
            raise ActivationError(
                "not_your_memory", "该候选操作的是他人的记忆，不能由你批准"
            )

        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        candidate.status = "active"
        db.commit()

    ops = [
        MemoryOp(
            operation=str(op.get("operation") or ""),
            memory_ref=str(op.get("memory_ref") or ""),
            scope=str(op.get("scope") or "user"),
            reason=str(op.get("reason") or ""),
            before=op.get("before"),
            after=op.get("after"),
        )
        for op in raw_ops
    ]
    outcome = apply_memory_ops(
        ops, candidate_id=candidate_id, user_id=user_id, approver=approver
    )
    return {"candidate_id": candidate_id, "user_id": user_id, **outcome}
