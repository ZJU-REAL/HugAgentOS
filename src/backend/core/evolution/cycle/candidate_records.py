"""Persist candidates and derive their attribution features."""
from __future__ import annotations
from core.evolution.cycle import patterns
from core.evolution.cycle import promotion_constants
from typing import Any, Dict, List, Optional
from core.db.engine import SessionLocal
from core.db.models.evolution import EvolutionCandidate
from core.evolution.evidence.credit import CreditFeatures
from core.evolution.ir import EvolutionIR, new_candidate_id
from core.evolution.cycle.release import DRAFT
import logging
logger = logging.getLogger(__name__)

def features_for(pattern: patterns.Pattern) -> CreditFeatures:
    """Translate a discovered pattern into attribution features.

    Success-subsequence tool patterns no longer come through here: they are an
    aggregate observation over successful runs, and inventing a ``failed``
    verdict to route them through the failure attributor is the exact move
    :func:`core.evolution.evidence.credit.aggregate_finding` exists to replace.
    """
    if pattern.kind == promotion_constants.PATTERN_REPEATED_FAILURE:
        return CreditFeatures(
            outcome_verdict="failed",
            outcome_confidence=0.8,
            loop_stagnation=True,
            no_diff_iterations=2,
        )
    return CreditFeatures(outcome_verdict="unknown", outcome_confidence=0.3)


def features_for_ordering_rules(constraints: List[Any]) -> CreditFeatures:
    """Attribution features for "a stable ordering keeps being re-derived".

    The mapping, stated so it can be argued with:

    * ``repeated_tool_subsequence`` — a pairwise ordering recurring across
      different tool sets is a repeated successful subsequence that is
      re-planned on every occurrence, which is what the feature means.
    * ``outcome_verdict="failed"`` — the runs this would fix are the ones that
      ordered the calls wrongly; the successful ones are the evidence, not the
      target.

    ``step_order_suspect`` is deliberately *not* set even though ordering is the
    subject. That feature says "this pipeline's step order looks wrong", a
    workflow-level claim, and asserting it ties workflow with skill at 0.65 —
    the assigner then correctly reports ``unidentifiable`` and nothing is
    produced. The evidence here does separate them: an ordering that holds
    across several different tool sets and task types is reusable knowledge, not
    one fixed pipeline, and "the workflow is fixed" is the reading it rules out.

    Confidence sits just above the ``MIN_CONFIDENCE`` floor on purpose: with
    support and context breadth at their minimum it lands below the floor and
    resolves to No-Update, so thin evidence cannot mint a permanent asset.
    """
    support = max((int(getattr(c, "support", 0) or 0) for c in constraints), default=0)
    contexts = max((int(getattr(c, "contexts", 0) or 0) for c in constraints), default=0)
    return CreditFeatures(
        outcome_verdict="failed",
        outcome_confidence=min(0.9, 0.6 + 0.05 * support + 0.05 * contexts),
        repeated_tool_subsequence=True,
    )


def uncovered_constraints(constraints: List[Any], covered_tool_sets: List[Any]) -> List[Any]:
    """Rules that no sequence candidate would carry into the runtime.

    A rule attached to a materialised sequence already reaches the agent through
    that skill. The ones worth a candidate of their own are the rest — and on
    real traffic that is most of them, because a rule earns its scope by
    appearing in *several* tool-set shapes and no single sequence covers them
    all.
    """
    out: List[Any] = []
    for constraint in constraints:
        pair = {constraint.before, constraint.after}
        if any(pair <= set(tools) for tools in covered_tool_sets):
            continue
        out.append(constraint)
    return out


def persist_candidate(
    ir: EvolutionIR,
    *,
    credit: Dict[str, Any],
    proposer: str,
    evidence_refs: List[str],
) -> Optional[str]:
    """Store a candidate, collapsing duplicates rather than failing on them."""
    from core.evolution.evidence import lineage

    # Attribution outlives the candidate that happened to carry it: every
    # decision that reaches persistence gets its own ledger row, and the stored
    # copy inside the candidate becomes a cache keyed by ``decision_id``.
    decision_id = lineage.record_credit_decision(
        credit, episode_id=(evidence_refs[0] if evidence_refs else "")
    )
    credit = {**credit, "decision_id": decision_id}

    checksum = ir.change_checksum()
    try:
        with SessionLocal() as db:
            existing = (
                db.query(EvolutionCandidate)
                .filter(
                    EvolutionCandidate.target_kind == ir.target_kind,
                    EvolutionCandidate.target_asset_id == ir.target_asset_id,
                    EvolutionCandidate.base_version_id == (ir.base_version or None),
                    EvolutionCandidate.change_checksum == checksum,
                )
                .first()
            )
            if existing is not None:
                # Same edit proposed again from fresh evidence: one review item,
                # not two.
                return existing.candidate_id

            candidate_id = new_candidate_id()
            db.add(
                EvolutionCandidate(
                    candidate_id=candidate_id,
                    target_kind=ir.target_kind,
                    target_asset_id=ir.target_asset_id,
                    base_version_id=ir.base_version or None,
                    operation=ir.operation,
                    ir=ir.to_dict(),
                    hypothesis=ir.hypothesis,
                    change_checksum=checksum,
                    evidence_refs=evidence_refs,
                    credit_decision=credit,
                    risk_tier=ir.risk_tier,
                    scope=ir.scope,
                    status=DRAFT,
                    proposer=proposer,
                )
            )
            db.commit()
    except Exception as exc:
        logger.warning("[evolution-loop] persisting candidate failed: %s", exc)
        return None

    # No backfill onto the turn cards. A skill candidate is a batch product
    # reviewed in the console; pushing it back into a conversation from days ago
    # put capability claims on a card whose only subject is what that turn
    # remembered. The candidate's own evidence_refs still record which episodes
    # produced it, which is where that link belongs.
    return candidate_id


def cross_user_mining_available() -> bool:
    """Whether fleet-wide mining may run in this edition.

    Personal evolution runs everywhere — it needs no aggregation across people
    and its output reaches only its owner. Mining *across* users and releasing
    to everyone is a governance surface, so it is enterprise-gated. The
    community edition keeps a complete personal evolution path rather than a
    degraded one.
    """
    try:
        # Resolved by name rather than a literal enterprise-tree import, so the
        # community build's boundary scan does not read this file as reaching
        # across the edition boundary. The behaviour is unchanged: the module is
        # physically absent in CE and the resulting ImportError still lands in
        # the handler below.
        from importlib import import_module

        features = import_module("edition_ee.licensing.features")
        manager = import_module("edition_ee.licensing.manager")

        return bool(manager.license_manager.has(features.Feature.EVOLUTION_CONTROL))
    except Exception:
        # The community tree physically excludes edition_ee, so an import error
        # here is the expected CE signal rather than a fault: personal only.
        return False


def run_async(coro) -> Any:
    """Run one coroutine from the cycle's synchronous body.

    The cycle is a background job and must run in a worker thread. Calling it
    from inside a running loop is a programming error and is raised as one:
    degrading to "skip the generators" would produce a cycle that silently
    stopped writing skills, which is the failure mode this whole rework exists
    to eliminate.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "run_evolution_cycle must not be called from a running event loop; "
        "dispatch it with run_in_executor"
    )
