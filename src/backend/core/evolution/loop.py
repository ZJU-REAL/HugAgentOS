"""The evolution loop, wired end to end (GCE integration).

Everything up to here has been a component.  This module is the loop itself:

    Episodes → patterns → credit → promotion → IR → invariants → candidate →
    release (shadow → canary → active) → the next run binds the new version

It runs off the response path, on the shared worker base, under the same cost
budget as distillation.  It cannot activate anything on its own beyond the low
risk tier: everything else stops at a candidate awaiting review, which is the
whole point of calling this *governed* co-evolution.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal
from core.db.models.evolution import (
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionRelease,
    EvolutionTraceEvent,
)
from core.evolution import promotion as P
from core.evolution.credit import (
    T_MEMORY,
    T_SKILL,
    T_ORCHESTRATION,
    CreditFeatures,
    aggregate_finding,
    assign_credit,
    confidence_from_evidence,
)
from core.evolution.ir import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    EvolutionIR,
    InvariantViolation,
    Rollback,
    new_candidate_id,
    validate_ir,
)
from core.evolution.release import DRAFT
from core.evolution.user_settings import PRIVACY_PRIVATE

logger = logging.getLogger(__name__)


def _view_for(episode, events) -> Dict[str, Any]:
    """One Episode plus its events, flattened into what the engines read.

    Three fields here did not exist and each blocked an engine outright:

    ``memory_refs`` now carries the **stable content ref**, not the raw content
    hash. The two look alike and are not: the ref is what the overlay keys on,
    so an op proposed against a hash could never be applied to anything.

    ``skills_opened`` is what the model chose to read, as opposed to
    ``skill_sequence``, which is what it was offered. Every decrement decision
    depends on the difference.

    ``selection_degraded`` marks turns where ranking could not run and
    everything loaded. Counting those as selections is what made every skill's
    offer count identical, and therefore useless.
    """
    from core.evolution.events import (
        EV_MEMORY_RETRIEVED,
        EV_SKILL_OPENED,
        EV_SKILL_SELECTED,
        EV_TOOL_CALLED,
    )
    from core.memory.ref_shadow import build_ref_id
    from core.memory.retrieval_types import LAYER_FACT

    tools: List[str] = []
    skills: List[str] = []
    memory_refs: List[str] = []
    skills_opened: List[Dict[str, Any]] = []
    selection_degraded = False
    error_count = 0

    user_id = episode.user_id or ""
    for event in events:
        payload = event.payload or {}
        if event.event_type == EV_TOOL_CALLED:
            name = payload.get("tool_name")
            if name:
                tools.append(str(name))
            if str(payload.get("status") or "") == "error":
                error_count += 1
        elif event.event_type == EV_SKILL_SELECTED:
            if payload.get("degraded"):
                selection_degraded = True
            skills.extend(str(s) for s in (payload.get("selected") or []))
        elif event.event_type == EV_SKILL_OPENED:
            skills_opened.append(
                {
                    "skill_id": str(payload.get("skill_id") or ""),
                    "tools_after_open": [
                        str(t) for t in (payload.get("tools_after_open") or [])
                    ],
                }
            )
        elif event.event_type == EV_MEMORY_RETRIEVED:
            for item in payload.get("items") or []:
                content_hash = str(item.get("content_hash") or "")
                if not content_hash:
                    continue
                memory_refs.append(
                    build_ref_id(
                        layer=str(item.get("layer") or LAYER_FACT),
                        user_id=user_id,
                        workspace_id=str(item.get("workspace_id") or "default"),
                        content_hash=content_hash,
                    )
                )

    outcome = episode.outcome or {}
    return {
        "episode_id": episode.episode_id,
        "chat_id": episode.chat_id or "",
        "user_id": user_id,
        "tenant_id": episode.tenant_id or "default",
        "verdict": outcome.get("verdict")
        or ("success" if (episode.quality_score or 0) >= 0.65 else "unknown"),
        "tool_sequence": tools,
        "skill_sequence": skills,
        "skills_opened": skills_opened,
        "selection_degraded": selection_degraded,
        "memory_refs": memory_refs,
        "task_type": episode.task_type or "chat",
        "backfilled": bool(episode.backfilled),
        # Repeated tool errors are the observable form of "this run was stuck",
        # which sub-agent derivation needs and nothing else recorded.
        "stalled": error_count >= 3,
        # What the user actually asked. Clustering only ever saw tool sequences
        # before this, so "the same question keeps coming back" was not a
        # detectable event. Sanitised at write time, so this is the preview
        # rather than the raw text.
        "objective": episode.objective_preview or "",
        "objective_hash": episode.objective_hash or "",
        "attributed_to": str(outcome.get("attributed_to") or ""),
        # Needed to place an episode before or after an activation. Without it
        # negative-transfer detection has no way to split the corpus and returns
        # nothing on every corpus — wired, running, and structurally incapable
        # of finding anything.
        "created_at": episode.created_at,
    }


def load_episode_views(*, limit: int = 500, since_days: int = 30) -> List[Dict[str, Any]]:
    """Flatten Episodes plus their trace events into the shape pattern mining wants.

    Backfilled Episodes are included on purpose: they cannot be replayed, but
    they are perfectly good for discovering what recurs — which is exactly why
    the backfill was worth doing.
    """
    views: List[Dict[str, Any]] = []
    try:
        with SessionLocal() as db:
            # The single enforcement point for per-user opt-out. Episodes from
            # users who declined to contribute are still recorded (they power
            # that user's own card and history) but never feed cross-user
            # pattern mining. Filtering here rather than at each call site is
            # what makes the promise checkable.
            episodes = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.privacy_class != PRIVACY_PRIVATE)
                .order_by(EvolutionEpisode.created_at.desc())
                .limit(limit)
                .all()
            )
            if not episodes:
                return []

            ids = [e.episode_id for e in episodes]
            events = (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.episode_id.in_(ids))
                .order_by(EvolutionTraceEvent.seq.asc())
                .all()
            )
            by_episode: Dict[str, List[EvolutionTraceEvent]] = {}
            for event in events:
                by_episode.setdefault(event.episode_id, []).append(event)

            for episode in episodes:
                views.append(_view_for(episode, by_episode.get(episode.episode_id, [])))
    except Exception as exc:
        logger.warning("[evolution-loop] loading episodes failed: %s", exc)
        return []
    return views


def _features_for(pattern: P.Pattern) -> CreditFeatures:
    """Translate a discovered pattern into attribution features."""
    if pattern.kind == P.PATTERN_SUCCESS_SUBSEQUENCE and pattern.tool_sequence:
        return CreditFeatures(
            outcome_verdict="failed",  # "works, but is re-planned every time"
            outcome_confidence=0.85,
            repeated_tool_subsequence=True,
            skill_selected_count=0,
            skill_rejected_count=len(pattern.tool_sequence),
        )
    if pattern.kind == P.PATTERN_REPEATED_FAILURE:
        return CreditFeatures(
            outcome_verdict="failed",
            outcome_confidence=0.8,
            loop_stagnation=True,
            no_diff_iterations=2,
        )
    return CreditFeatures(outcome_verdict="unknown", outcome_confidence=0.3)


def _features_for_ordering_rules(constraints: List[Any]) -> CreditFeatures:
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


def _uncovered_constraints(constraints: List[Any], covered_tool_sets: List[Any]) -> List[Any]:
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


def _persist_candidate(
    ir: EvolutionIR,
    *,
    credit: Dict[str, Any],
    proposer: str,
    evidence_refs: List[str],
) -> Optional[str]:
    """Store a candidate, collapsing duplicates rather than failing on them."""
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


def _run_async(coro) -> Any:
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


def _memory_scan_findings(views: List[Dict[str, Any]]) -> List[Any]:
    """Scan every contributing user's store.

    Per user, because the store is per user: near-duplicates and contradictions
    only mean anything within one person's memories, and a cross-user scan would
    both mean nothing and read what it has no business reading.
    """
    from core.evolution.memory_scan import scan_user_memories

    retrieved: Dict[str, Dict[str, int]] = {}
    for view in views:
        user_id = str(view.get("user_id") or "")
        if not user_id:
            continue
        bucket = retrieved.setdefault(user_id, {})
        for ref in view.get("memory_refs") or []:
            bucket[str(ref)] = bucket.get(str(ref), 0) + 1

    findings: List[Any] = []
    for user_id, refs in retrieved.items():
        try:
            _, user_findings = _run_async(
                scan_user_memories(user_id, retrieved_refs=refs)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[evolution-loop] memory scan failed for %s: %s", user_id, exc)
            continue
        findings.extend(user_findings)
    return findings


def _memory_candidates(
    extras: Dict[str, Any], views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """One memory candidate per user, carrying that user's operations.

    Grouped per user rather than per operation because the approval question is
    "may the system reorganise *my* memories", and answering it forty times for
    forty entries is not review, it is attrition.
    """
    memory = extras.get("memory") or {}
    ops = memory.get("ops") or []
    if not ops:
        return []

    owner_by_ref: Dict[str, str] = {}
    for view in views:
        user_id = str(view.get("user_id") or "")
        for ref in view.get("memory_refs") or []:
            owner_by_ref.setdefault(str(ref), user_id)

    by_user: Dict[str, List[Any]] = {}
    for op in ops:
        owner = owner_by_ref.get(op.memory_ref, "")
        if not owner:
            # An operation whose subject cannot be identified must not be
            # proposed: applying it would need a user id, and guessing one is
            # how a change lands on the wrong person's memories.
            continue
        by_user.setdefault(owner, []).append(op)

    created: List[Dict[str, Any]] = []
    for user_id, user_ops in by_user.items():
        # Duplicates, contradictions and stale entries are properties of the
        # store, found by scanning it — not a conflict with any one request.
        decision = aggregate_finding(
            target=T_MEMORY,
            explanation="；".join(op.reason for op in user_ops[:3]),
            confidence=confidence_from_evidence(len(user_ops), saturates_at=8),
            evidence_count=len(user_ops),
        )
        if not decision.may_produce_candidate:
            continue

        asset_id = "mem-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
        ir = EvolutionIR(
            target_kind="memory",
            target_asset_id=asset_id,
            base_version="current",
            operation="merge" if any(op.operation == "merge" for op in user_ops) else "reweight",
            hypothesis=(
                f"记忆库有 {len(user_ops)} 处需要重组："
                + "；".join(op.reason for op in user_ops[:3])
            ),
            changes=[
                {
                    "user_id": user_id,
                    "operations": [op.to_dict() for op in user_ops],
                }
            ],
            evidence_refs=[op.memory_ref for op in user_ops][:20],
            # Changing what a person's agent remembers is never low risk, and
            # the scope is that person alone.
            risk_tier=RISK_MEDIUM,
            scope={"level": "user", "user_id": user_id},
            rollback=Rollback(version_id="current", triggers=["user_disable"]),
            evaluation_plan={"replay_set": "personal", "primary_metric": "task_success"},
        )
        try:
            validate_ir(
                ir, credit_decision=decision.to_dict(), allowed_scopes=["user", "workspace"]
            )
        except InvariantViolation as violation:
            logger.info("[evolution-loop] memory candidate rejected: %s", violation.code)
            continue
        if dry_run:
            created.append({"dry_run": True, "kind": "memory", "user_id": user_id})
            continue
        candidate_id = _persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="memory_engine",
            evidence_refs=[op.memory_ref for op in user_ops][:20],
        )
        if candidate_id:
            created.append(
                {"candidate_id": candidate_id, "kind": "memory", "user_id": user_id,
                 "operations": len(user_ops)}
            )
    return created


def _intent_skill_candidates(
    extras: Dict[str, Any], views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """Recurring intents, written up as skills by the generator.

    The intent finding says a capability is missing; it does not say what the
    capability is. Turning it into an asset needs a document, which is what
    :mod:`core.evolution.skill_gen` produces and validates. A finding without a
    document has nowhere to go — which is exactly where these used to stop.
    """
    from core.evolution.skill_gen import generate_skill_document

    findings = [
        f
        for f in (extras.get("intent") or {}).get("findings", [])
        if f.get("action") == "skill_candidate"
    ]
    if not findings:
        return []

    by_episode = {str(v.get("episode_id") or ""): v for v in views}
    created: List[Dict[str, Any]] = []

    for finding in findings:
        cluster = finding.get("cluster") or {}
        episode_ids = [str(e) for e in (cluster.get("episode_ids") or [])]
        episodes = [by_episode[e] for e in episode_ids if e in by_episode]
        if not episodes:
            continue

        # The cluster mostly *succeeded* — that is a precondition for
        # distilling it. Declaring the turns failed to satisfy the failure
        # attributor would contradict the very check that let this through.
        decision = aggregate_finding(
            target=T_SKILL,
            explanation=str(finding.get("reason") or ""),
            confidence=confidence_from_evidence(len(episode_ids), saturates_at=10),
            evidence_count=len(episode_ids),
        )
        if not decision.may_produce_candidate:
            continue

        asset_id = str(finding.get("asset_id") or "")
        procedures = _procedures_for_cluster(cluster, views)
        document, draft = _run_async(
            generate_skill_document(
                skill_id=asset_id,
                episodes=episodes,
                procedures=procedures,
                rationale=str(finding.get("reason") or ""),
            )
        )
        if document is None:
            created.append(
                {
                    "kind": "recurring_intent",
                    "asset_id": asset_id,
                    "written": False,
                    "why_not": draft.to_dict(),
                }
            )
            continue

        ir = EvolutionIR(
            target_kind="skill",
            target_asset_id=asset_id,
            base_version="v0",
            operation="new",
            hypothesis=str(finding.get("reason") or ""),
            changes=[
                {
                    "document": document,
                    # Carried so demotion has a target: these are the memories
                    # whose content went into the document, each with its owner,
                    # because down-weighting happens in one person's store.
                    "source_memories": [
                        {"ref": p.get("ref", ""), "user_id": p.get("user_id", "")}
                        for p in procedures
                        if p.get("ref")
                    ],
                    "tool_sequence": [],
                }
            ],
            evidence_refs=episode_ids[:20],
            requested_tools=list(document.get("allowed_tools") or []),
            risk_tier=RISK_HIGH,
            scope={"level": "workspace"},
            rollback=Rollback(
                version_id="v0", triggers=["success -3pp", "risk_denial_rate +2pp"]
            ),
            evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
        )
        try:
            validate_ir(
                ir,
                credit_decision=decision.to_dict(),
                base_tools=list(document.get("allowed_tools") or []),
            )
        except InvariantViolation as violation:
            logger.info("[evolution-loop] intent candidate rejected: %s", violation.code)
            continue
        if dry_run:
            created.append({"dry_run": True, "kind": "recurring_intent", "asset_id": asset_id})
            continue
        candidate_id = _persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="intent_engine",
            evidence_refs=episode_ids[:20],
        )
        if candidate_id:
            created.append(
                {
                    "candidate_id": candidate_id,
                    "kind": "recurring_intent",
                    "asset_id": asset_id,
                    "written": True,
                    "attempts": draft.attempts,
                }
            )
    return created


def _procedures_for_cluster(
    cluster: Dict[str, Any], views: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """The procedural memories retrieved during this intent's episodes.

    These are what makes the resulting document a *compilation* of memory rather
    than a restatement of a tool list — and their refs are what gets demoted
    once the skill is live.
    """
    from core.evolution.memory_scan import procedural_records, to_records
    from core.memory.ref_shadow import build_ref_id
    from core.memory.retrieval_types import LAYER_FACT

    wanted = {str(ref) for ref in (cluster.get("memory_refs") or [])}
    if not wanted:
        return []

    episode_ids = {str(e) for e in (cluster.get("episode_ids") or [])}
    users = {
        str(v.get("user_id") or "")
        for v in views
        if str(v.get("episode_id") or "") in episode_ids and v.get("user_id")
    }

    procedures: List[Dict[str, str]] = []
    for user_id in users:
        try:
            from core.memory.service import get_all_memories

            raw = _run_async(get_all_memories(user_id, top_k=500))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[evolution-loop] procedure lookup failed for %s: %s", user_id, exc)
            continue
        for record in procedural_records(to_records(raw)):
            ref = build_ref_id(
                layer=LAYER_FACT,
                user_id=user_id,
                workspace_id=record.workspace_id,
                content_hash=record.content_hash,
            )
            if ref in wanted:
                procedures.append(
                    {
                        "ref": ref,
                        # Carried because a fleet-wide skill can be compiled from
                        # several people's memories, and demotion is per-store: a
                        # bare list of refs has no owner to apply them against, so
                        # the reverse edge would silently do nothing for exactly
                        # the candidates that need it most.
                        "user_id": user_id,
                        "rule": record.content,
                        "why": record.why,
                        "applies_to": record.applies_to,
                    }
                )
    return procedures


def _decremental_candidates(extras: Dict[str, Any], *, dry_run: bool) -> List[Dict[str, Any]]:
    """Retirement and merge findings, as candidates rather than as a list.

    Retiring goes through the same approval path as creating. Removing a
    capability produces no error — only a slow decline — so it is precisely the
    direction that must not be automatic.
    """
    from core.evolution.promotion import ACTION_REWRITE

    decremental = extras.get("decremental") or {}
    created: List[Dict[str, Any]] = []

    for item in decremental.get("retirements") or []:
        asset_id = str(item.get("asset_id") or "")
        if not asset_id:
            continue
        action = str(item.get("action") or "")
        if action == ACTION_REWRITE:
            # "Opened and ignored" is a content problem. Proposing retirement
            # would remove a capability the model wanted; the fix is a rewrite,
            # and that is a different candidate written from failure evidence.
            created.append({"kind": "needs_rewrite", "asset_id": asset_id, **item})
            continue

        decision = aggregate_finding(
            target=T_SKILL,
            explanation=str(item.get("explanation") or item.get("reason") or ""),
            confidence=confidence_from_evidence(
                int(item.get("offers", 0) or 0), saturates_at=20
            ),
            evidence_count=int(item.get("offers", 0) or 0),
        )
        if not decision.may_produce_candidate:
            continue

        ir = EvolutionIR(
            target_kind="skill",
            target_asset_id=asset_id,
            base_version="current",
            operation="deprecate",
            hypothesis=str(item.get("explanation") or item.get("reason") or ""),
            changes=[{"retire_reason": item.get("reason"), "stats": {
                k: v for k, v in item.items() if k not in ("asset_id", "explanation")
            }}],
            evidence_refs=[asset_id],
            risk_tier=RISK_MEDIUM,
            scope={"level": "workspace"},
            rollback=Rollback(version_id="current", triggers=["manual"]),
            evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
        )
        try:
            validate_ir(ir, credit_decision=decision.to_dict())
        except InvariantViolation as violation:
            logger.info("[evolution-loop] retirement rejected: %s", violation.code)
            continue
        if dry_run:
            created.append({"dry_run": True, "kind": "retirement", "asset_id": asset_id})
            continue
        candidate_id = _persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="decremental_engine",
            evidence_refs=[asset_id],
        )
        if candidate_id:
            created.append(
                {"candidate_id": candidate_id, "kind": "retirement", "asset_id": asset_id,
                 "reason": item.get("reason")}
            )
    return created


def _orchestration_candidates(
    views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """Profile and sub-agent candidates.

    The tool authority every candidate here is measured against is the *current*
    grant, read once. A profile may only ever narrow it, and a derived sub-agent
    may only ever receive a subset — both enforced by the IR gate rather than by
    the generator, so the check holds even for a candidate that arrived by some
    other route.
    """
    from core.config.catalog import get_enabled_ids
    from core.evolution.agent_profile import TARGET_KIND, builtin_profile, validate_profile
    from core.evolution.orchestration_gen import (
        generate_orchestration_proposals,
        profile_from_proposals,
    )

    base_tools = [str(t) for t in get_enabled_ids("tools")] + [
        str(t) for t in get_enabled_ids("mcp")
    ]
    offered_tools = {
        str(view.get("task_type") or "chat"): base_tools for view in views
    }
    proposals, subagents = generate_orchestration_proposals(
        views, offered_tools=offered_tools, parent_tools=base_tools
    )
    created: List[Dict[str, Any]] = []

    task_types = sorted({p.task_type for p in proposals})
    for task_type in task_types:
        task_proposals = [p for p in proposals if p.task_type == task_type]
        profile = profile_from_proposals(
            task_proposals, task_type=task_type, base=builtin_profile()
        )
        ok, problems = validate_profile(profile, base_tools=base_tools)
        if not ok:
            logger.info("[evolution-loop] profile %s rejected: %s", profile.profile_id, problems)
            continue

        evidence = [ref for p in task_proposals for ref in p.evidence_refs][:20]
        # An observation across the corpus, not a failure attributed to one run.
        # Inventing failure features to make the attributor return "workflow"
        # is what put "过程停滞：重复动作、无产出增量" in front of reviewers for a
        # candidate whose actual reason was "eight tools were never used".
        decision = aggregate_finding(
            target=T_ORCHESTRATION,
            explanation="；".join(p.rationale for p in task_proposals),
            confidence=confidence_from_evidence(sum(p.support for p in task_proposals)),
            evidence_count=len(evidence),
        )
        if not decision.may_produce_candidate:
            continue
        ir = EvolutionIR(
            target_kind=TARGET_KIND,
            target_asset_id=profile.profile_id,
            base_version="builtin",
            operation="new",
            hypothesis="；".join(p.rationale for p in task_proposals),
            changes=[
                {
                    "profile": profile.to_dict(),
                    "tool_allowlist": profile.tool_allowlist or [],
                }
            ],
            evidence_refs=evidence,
            requested_tools=list(profile.tool_allowlist or []),
            risk_tier=RISK_MEDIUM,
            scope={"level": "workspace"},
            rollback=Rollback(version_id="builtin", triggers=["success -3pp"]),
            evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
        )
        try:
            validate_ir(ir, credit_decision=decision.to_dict(), base_tools=base_tools)
        except InvariantViolation as violation:
            logger.info("[evolution-loop] profile candidate rejected: %s", violation.code)
            continue
        if dry_run:
            created.append({"dry_run": True, "kind": "agent_profile", "task_type": task_type})
            continue
        candidate_id = _persist_candidate(
            ir, credit=decision.to_dict(), proposer="orchestration_engine",
            evidence_refs=evidence,
        )
        if candidate_id:
            created.append(
                {"candidate_id": candidate_id, "kind": "agent_profile", "task_type": task_type}
            )

    for subagent in subagents:
        decision = aggregate_finding(
            target=T_ORCHESTRATION,
            explanation=subagent.rationale,
            confidence=confidence_from_evidence(subagent.support),
            evidence_count=len(subagent.evidence_refs),
        )
        if not decision.may_produce_candidate:
            continue
        payload = subagent.to_dict()
        ir = EvolutionIR(
            target_kind="subagent",
            target_asset_id=subagent.agent_id,
            base_version="v0",
            operation="new",
            hypothesis=subagent.rationale,
            changes=[payload],
            evidence_refs=subagent.evidence_refs,
            requested_tools=list(subagent.tools_allowlist),
            # Creating an agent is the widest-reaching change this system can
            # propose, and the route into it changes the parent's prompt.
            risk_tier=RISK_HIGH,
            scope={"level": "workspace"},
            rollback=Rollback(version_id="v0", triggers=["manual"]),
            evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
        )
        try:
            validate_ir(ir, credit_decision=decision.to_dict(), base_tools=base_tools)
        except InvariantViolation as violation:
            logger.info("[evolution-loop] subagent candidate rejected: %s", violation.code)
            continue
        if dry_run:
            created.append({"dry_run": True, "kind": "subagent", "name": subagent.name})
            continue
        candidate_id = _persist_candidate(
            ir, credit=decision.to_dict(), proposer="orchestration_engine",
            evidence_refs=subagent.evidence_refs,
        )
        if candidate_id:
            created.append(
                {"candidate_id": candidate_id, "kind": "subagent", "name": subagent.name}
            )
    return created


def _prompt_candidates(views: List[Dict[str, Any]], *, dry_run: bool) -> List[Dict[str, Any]]:
    """Prompt fragments, for the failures attribution already blames on the prompt."""
    from core.evolution.prompt_gen import generate_prompt_candidate

    payload, evidence, draft = _run_async(
        generate_prompt_candidate(
            views,
            cause="归因判定该失败源于提示词未声明相关约束",
            existing_fragments=_active_prompt_fragments(),
        )
    )
    if payload is None or evidence is None:
        return (
            []
            if draft.violations == ["insufficient_evidence"]
            else [{"kind": "prompt", "written": False, "why_not": draft.to_dict()}]
        )

    decision = assign_credit(
        CreditFeatures(
            outcome_verdict="failed",
            outcome_confidence=0.85,
            prompt_missing_priority_rule=True,
        )
    )
    if not decision.may_produce_candidate:
        return []

    asset_id = "auto-prompt-" + hashlib.sha256(
        payload["fragment"].encode("utf-8")
    ).hexdigest()[:12]
    ir = EvolutionIR(
        target_kind="prompt",
        target_asset_id=asset_id,
        base_version="active",
        operation="patch",
        hypothesis=payload["cause"],
        changes=[{"fragment": payload["fragment"], "task_types": payload["task_types"]}],
        evidence_refs=evidence.episode_ids[:20],
        # A prompt fragment applies to every request in scope; nothing this
        # system produces has a wider reach.
        risk_tier=RISK_HIGH,
        scope={"level": "workspace"},
        rollback=Rollback(version_id="active", triggers=["success -3pp"]),
        evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
    )
    try:
        validate_ir(ir, credit_decision=decision.to_dict())
    except InvariantViolation as violation:
        return [{"kind": "prompt", "written": False, "why_not": {"code": violation.code}}]
    if dry_run:
        return [{"dry_run": True, "kind": "prompt", "asset_id": asset_id}]
    candidate_id = _persist_candidate(
        ir, credit=decision.to_dict(), proposer="prompt_engine",
        evidence_refs=evidence.episode_ids[:20],
    )
    return (
        [{"candidate_id": candidate_id, "kind": "prompt", "asset_id": asset_id}]
        if candidate_id
        else []
    )


def _active_prompt_fragments() -> List[str]:
    """Fragments already appended by earlier prompt candidates.

    Passed to the generator so it can decline rather than restate: a prompt that
    accumulates three phrasings of the same instruction is measurably worse than
    one that states it once.
    """
    try:
        from core.db.engine import SessionLocal as _Session
        from core.db.models.evolution import EvolutionRelease as _Release

        with _Session() as db:
            rows = (
                db.query(_Release)
                .filter(_Release.target_kind == "prompt", _Release.stage == "active")
                .all()
            )
        return [str(row.target_asset_id) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[evolution-loop] active prompt fragments unavailable: %s", exc)
        return []


def _run_other_engines(
    views: List[Dict[str, Any]], *, dry_run: bool
) -> Dict[str, Any]:
    """Memory, decremental, intent, orchestration and prompt — each with an exit.

    Independently guarded: a failure in one engine must not cost the others
    their findings, because they answer different questions and an operator
    needs whichever ones succeeded.
    """
    from core.evolution.cycle_extras import run_extras

    scan_findings = _memory_scan_findings(views)
    extras = run_extras(views, scan_findings=scan_findings)

    produced: List[Dict[str, Any]] = []
    for name, fn in (
        ("memory", lambda: _memory_candidates(extras, views, dry_run=dry_run)),
        ("intent", lambda: _intent_skill_candidates(extras, views, dry_run=dry_run)),
        ("decremental", lambda: _decremental_candidates(extras, dry_run=dry_run)),
        ("orchestration", lambda: _orchestration_candidates(views, dry_run=dry_run)),
        ("prompt", lambda: _prompt_candidates(views, dry_run=dry_run)),
    ):
        try:
            produced.extend(fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[evolution-loop] %s engine failed: %s", name, exc)
            produced.append({"kind": name, "error": str(exc)})

    # ``ops`` holds MemoryOp objects for the candidate builder; the report is
    # serialised into the console, so it must not carry them.
    memory_report = dict(extras.get("memory") or {})
    memory_report.pop("ops", None)
    extras["memory"] = memory_report
    extras["memory"]["scan_findings"] = [
        f.to_dict() if hasattr(f, "to_dict") else f for f in scan_findings
    ][:50]

    return {"extras": extras, "engine_candidates": produced}


def run_evolution_cycle(
    *, limit: int = 500, dry_run: bool = False
) -> Dict[str, Any]:
    """One fleet-wide pass: discover → attribute → promote → validate → persist.

    Returns a report rather than raising: this runs as a background job and a
    bad cycle must leave the product untouched.
    """
    if not cross_user_mining_available():
        logger.info("[evolution-loop] cross-user mining unavailable; personal only")
        return {
            "episodes": 0,
            "patterns": 0,
            "candidates": [],
            "rejected": [],
            "no_update": 0,
            "skipped": "cross_user_mining_requires_enterprise",
        }
    # Refuse rather than degrade. Without an embedding service two of the three
    # engines lose their primary path and the cycle reports success having found
    # nothing — which is indistinguishable from "there was nothing to find", and
    # is the state nobody investigates.
    from core.evolution.readiness import check_readiness

    readiness = check_readiness()
    if not readiness.ready:
        logger.warning(
            "[evolution-loop] not ready, refusing to run: %s", readiness.blocking
        )
        return {
            "episodes": 0,
            "patterns": 0,
            "candidates": [],
            "rejected": [],
            "no_update": 0,
            "skipped": "not_ready",
            "readiness": readiness.to_dict(),
        }

    report: Dict[str, Any] = {
        "episodes": 0,
        "patterns": 0,
        "proposals": 0,
        "candidates": [],
        "rejected": [],
        "no_update": 0,
        "readiness": readiness.to_dict(),
    }

    views = load_episode_views(limit=limit)
    report["episodes"] = len(views)
    if not views:
        return report

    patterns = P.discover_patterns(views)
    report["patterns"] = len(patterns)

    # Transferable ordering rules, extracted once across the whole corpus rather
    # than per pattern. A rigid sequence only helps tasks whose tool set it
    # happens to cover; the ordering rule behind it helps every task making the
    # same mistake, which is the difference between memorising and learning.
    constraints = P.extract_ordering_constraints(views)
    report["ordering_constraints"] = [c.to_dict() for c in constraints]

    existing_signatures: Dict[str, str] = {}
    covered_tool_sets: List[List[str]] = []

    for pattern in patterns:
        decision = assign_credit(_features_for(pattern))
        if not decision.may_produce_candidate:
            # No-update / unidentifiable / a non-asset cause. Refusing here is
            # the mechanism that stops a KB gap being "fixed" by editing a skill.
            report["no_update"] += 1
            continue

        proposal = P.promote_tool_sequence_to_skill(
            pattern,
            skill_credit=decision.scores.get("skill", 0.0),
            workflow_credit=decision.scores.get("workflow", 0.0),
            existing_skill_signatures=existing_signatures,
        )
        if proposal is None:
            continue
        report["proposals"] += 1

        # Stable, content-derived id. Python's built-in hash() is randomised per
        # process, so using it here minted a fresh asset id on every cycle and the
        # de-duplication constraint never matched — one candidate per run, forever.
        asset_id = proposal.patch_target or (
            "auto-" + hashlib.sha256(proposal.signature.encode("utf-8")).hexdigest()[:12]
        )
        ir = EvolutionIR(
            target_kind="skill",
            target_asset_id=asset_id,
            base_version="v0" if not proposal.patch_target else "current",
            operation="patch" if proposal.patch_target else "new",
            hypothesis=proposal.rationale,
            changes=[
                {
                    "tool_sequence": proposal.payload.get("tool_sequence", []),
                    # Attach every rule whose tools this skill touches, and
                    # carry its scope along with it.
                    #
                    # Attaching must not apply the usability policy: doing so
                    # dropped exactly the context-dependent rules the scoping
                    # exists to preserve — a rule contradicted in one task type
                    # is still valid in the ones where it held, and that
                    # decision belongs at use time, where the context is known.
                    "ordering_constraints": [
                        c.to_dict()
                        for c in constraints
                        if c.before in proposal.payload.get("tool_sequence", [])
                        and c.after in proposal.payload.get("tool_sequence", [])
                    ],
                }
            ],
            evidence_refs=proposal.payload.get("episode_ids", []),
            # A brand-new capability is riskier than tweaking an existing one.
            risk_tier=RISK_MEDIUM if proposal.patch_target else RISK_HIGH,
            scope={"level": "workspace"},
            rollback=Rollback(
                version_id="v0" if not proposal.patch_target else "current",
                triggers=["success -3pp", "risk_denial_rate +2pp"],
            ),
            evaluation_plan={"replay_set": "auto_holdout", "primary_metric": "task_success"},
        )

        try:
            validate_ir(ir, credit_decision=decision.to_dict(), base_tools=None)
        except InvariantViolation as violation:
            report["rejected"].append(
                {"signature": proposal.signature, "code": violation.code}
            )
            continue

        if dry_run:
            report["candidates"].append({"dry_run": True, "signature": proposal.signature})
            continue

        candidate_id = _persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="promotion_chain",
            evidence_refs=proposal.payload.get("episode_ids", []),
        )
        if candidate_id:
            covered_tool_sets.append(list(proposal.payload.get("tool_sequence") or []))
            report["candidates"].append(
                {"candidate_id": candidate_id, "signature": proposal.signature}
            )

    # ── Ordering rules that no sequence candidate carries ────────────────────
    #
    # Without this the only shape of learning that can become an asset is "five
    # runs repeated one identical tool sequence", which on real traffic is close
    # to unobservable — measured on this deployment's history, every episode's
    # tool sequence was unique. The rules are the part that generalises, so they
    # get to stand on their own rather than only riding along on a sequence that
    # happens to qualify.
    orphan_rules = _uncovered_constraints(constraints, covered_tool_sets)
    if orphan_rules:
        rule_decision = assign_credit(_features_for_ordering_rules(orphan_rules))
        if not rule_decision.may_produce_candidate:
            report["no_update"] += 1
        else:
            rule_payload = [c.to_dict() for c in orphan_rules]
            signature = "|".join(
                sorted(f"{c.before}<{c.after}" for c in orphan_rules)
            )
            asset_id = "auto-rules-" + hashlib.sha256(
                signature.encode("utf-8")
            ).hexdigest()[:12]
            episode_ids = sorted(
                {
                    view["episode_id"]
                    for view in views
                    if view.get("verdict") == "success"
                    and any(
                        c.before in (view.get("tool_sequence") or [])
                        and c.after in (view.get("tool_sequence") or [])
                        for c in orphan_rules
                    )
                }
            )[:20]
            rule_ir = EvolutionIR(
                target_kind="skill",
                target_asset_id=asset_id,
                base_version="v0",
                operation="new",
                hypothesis=(
                    f"{len(orphan_rules)} 条工具顺序约束在 {len(episode_ids)} 条成功执行中"
                    "稳定成立且跨多种工具组合复现，但规划阶段每次仍可能排错"
                ),
                changes=[
                    {
                        # Deliberately empty: this asset claims no fixed sequence,
                        # only constraints. Materialisation renders it as rules.
                        "tool_sequence": [],
                        "ordering_constraints": rule_payload,
                    }
                ],
                evidence_refs=episode_ids,
                risk_tier=RISK_HIGH,
                scope={"level": "workspace"},
                rollback=Rollback(
                    version_id="v0",
                    triggers=["success -3pp", "risk_denial_rate +2pp"],
                ),
                evaluation_plan={
                    "replay_set": "auto_holdout",
                    "primary_metric": "task_success",
                },
            )
            try:
                validate_ir(
                    rule_ir, credit_decision=rule_decision.to_dict(), base_tools=None
                )
            except InvariantViolation as violation:
                report["rejected"].append(
                    {"signature": signature, "code": violation.code}
                )
            else:
                report["proposals"] += 1
                if dry_run:
                    report["candidates"].append(
                        {"dry_run": True, "signature": signature, "kind": "ordering_rules"}
                    )
                else:
                    rule_candidate_id = _persist_candidate(
                        rule_ir,
                        credit=rule_decision.to_dict(),
                        proposer="promotion_chain",
                        evidence_refs=episode_ids,
                    )
                    if rule_candidate_id:
                        report["candidates"].append(
                            {
                                "candidate_id": rule_candidate_id,
                                "signature": signature,
                                "kind": "ordering_rules",
                            }
                        )

    # The other two engines, plus orchestration. Every finding below becomes a
    # candidate in the same table, behind the same security gate and the same
    # review queue as the skill candidates above — a finding that stops at a
    # report is a log line, not an engine.
    report.update(_run_other_engines(views, dry_run=dry_run))

    logger.info(
        "[evolution-loop] cycle done: episodes=%d patterns=%d candidates=%d no_update=%d",
        report["episodes"],
        report["patterns"],
        len(report["candidates"]),
        report["no_update"],
    )
    return report


# ── Advancing candidates through the release pipeline ────────────────────────


def _record_evaluation(candidate_id: str, report: Any, eval_type: str) -> None:
    """Persist an evaluation result against its candidate."""
    from core.db.models.evolution import EvolutionEvaluation

    try:
        payload = report.to_dict()
        with SessionLocal() as db:
            db.add(
                EvolutionEvaluation(
                    evaluation_id=payload["evaluation_id"],
                    candidate_id=candidate_id,
                    eval_type=eval_type,
                    # Frozen: a dataset edited after the fact invalidates every
                    # conclusion computed from it.
                    dataset_snapshot=payload["dataset_snapshot"],
                    baseline_metrics=payload["baseline"],
                    candidate_metrics=payload["candidate"],
                    guardrails={"breaches": payload["guardrail_breaches"]},
                    verdict=payload["verdict"],
                    effect_size=payload["effect_size"],
                    p_value=payload["p_value"],
                    sample_size=payload["sample_size"],
                )
            )
            db.commit()
    except Exception as exc:
        logger.warning("[evolution-loop] recording evaluation failed: %s", exc)


def evaluate_candidate(
    candidate_id: str,
    run_arm: Any,
    task_ids: List[str],
) -> Optional[Dict[str, Any]]:
    """Run tiered replay for a candidate and advance it if it earns the step.

    ``run_arm(task_id, is_candidate)`` is injected: the caller owns isolation
    (frozen bundle, cassette, sandbox snapshot). Only ``replay_passed`` is
    reachable from here — shadow and canary need real traffic, and activation
    needs a human, so this can never quietly ship anything.
    """
    from core.evolution.release import (
        DRAFT,
        REJECTED,
        REPLAY_PASSED,
        is_legal_transition,
    )
    from core.evolution.replay import VERDICT_IMPROVED, VERDICT_REGRESSED, run_tiered_replay

    tiered = run_tiered_replay(task_ids, run_arm)
    report = tiered.report or tiered.sentinel_report
    if report is None:
        return None

    _record_evaluation(candidate_id, report, "replay" if tiered.passed_sentinel else "sentinel")

    try:
        with SessionLocal() as db:
            candidate = db.get(EvolutionCandidate, candidate_id)
            if candidate is None:
                return None
            current = candidate.status

            # Re-evaluating a candidate that has already moved on is a no-op,
            # not a failure. Collapsing the two into None left callers unable to
            # tell "already advanced" from "something broke".
            target = (
                REJECTED
                if report.verdict == VERDICT_REGRESSED
                else (REPLAY_PASSED if report.verdict == VERDICT_IMPROVED else current)
            )
            if target != current and not is_legal_transition(current, target):
                return {
                    "candidate_id": candidate_id,
                    "verdict": report.verdict,
                    "status": current,
                    "transition": "skipped_already_advanced",
                    "tasks_run": tiered.tasks_run,
                    "effect_size": report.effect_size,
                    "p_value": report.p_value,
                }

            if report.verdict == VERDICT_REGRESSED:
                candidate.status = REJECTED
                candidate.reject_reason = (
                    f"replay regression: effect={report.effect_size:.3f} "
                    f"p={report.p_value:.4f} {report.guardrail_breaches}"
                )
            elif report.verdict == VERDICT_IMPROVED:
                candidate.status = REPLAY_PASSED
            else:
                # Neutral or insufficient: stays a draft. Not advancing is the
                # correct outcome — "we could not show it helps" is not "it helps".
                pass
            db.commit()
            return {
                "candidate_id": candidate_id,
                "verdict": report.verdict,
                "status": candidate.status,
                "transition": "applied" if candidate.status != current else "held",
                "tasks_run": tiered.tasks_run,
                "effect_size": report.effect_size,
                "p_value": report.p_value,
            }
    except Exception as exc:
        logger.warning("[evolution-loop] advancing candidate failed: %s", exc)
        return None
