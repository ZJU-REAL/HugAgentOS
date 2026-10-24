"""Memory and intent candidates built from per-user memory scans."""
from __future__ import annotations
import hashlib
from typing import Any, Dict, List
from core.evolution.evidence.credit import T_MEMORY, T_SKILL, aggregate_finding, confidence_from_evidence
from core.evolution.ir import RISK_HIGH, RISK_MEDIUM, EvolutionIR, InvariantViolation, Rollback, validate_ir
import logging
from core.evolution.cycle import candidate_records

logger = logging.getLogger(__name__)

def memory_scan_findings(views: List[Dict[str, Any]]) -> List[Any]:
    """Scan every contributing user's store.

    Per user, because the store is per user: near-duplicates and contradictions
    only mean anything within one person's memories, and a cross-user scan would
    both mean nothing and read what it has no business reading.
    """
    from core.evolution.generators.memory_scan import scan_user_memories

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
            _, user_findings = candidate_records.run_async(scan_user_memories(user_id, retrieved_refs=refs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[evolution-loop] memory scan failed for %s: %s", user_id, exc)
            continue
        findings.extend(user_findings)
    return findings


def memory_candidates(
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
        candidate_id = candidate_records.persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="memory_engine",
            evidence_refs=[op.memory_ref for op in user_ops][:20],
        )
        if candidate_id:
            created.append(
                {
                    "candidate_id": candidate_id,
                    "kind": "memory",
                    "user_id": user_id,
                    "operations": len(user_ops),
                }
            )
    return created


def intent_skill_candidates(
    extras: Dict[str, Any], views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """Recurring intents, compiled through the unified skill candidate engine.

    The intent finding says a capability is missing; it does not say what the
    capability is. The engine resolves the cluster's evidence into an immutable
    pack, derives the deterministic structure (tools, boundaries, dependencies),
    has the model write only the document, and validates the layered contract —
    the same chain the tool-sequence path uses, so identical evidence can no
    longer produce structurally different skills depending on which engine
    noticed it first.
    """
    from core.evolution.generators.skill_candidate_engine import STATUS_CREATED, STATUS_DRY_RUN, STATUS_NOT_WRITTEN, STATUS_REJECTED, SkillCandidateSpec, create_skill_candidate

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
        spec = SkillCandidateSpec(
            asset_id=asset_id,
            proposer="intent_engine",
            hypothesis=str(finding.get("reason") or ""),
            views=episodes,
            intent_cluster=cluster,
            procedures=procedures_for_cluster(cluster, views),
            operation="new",
            base_version="v0",
            risk_tier=RISK_HIGH,
            scope={"level": "workspace"},
            tenant_id=str(episodes[0].get("tenant_id") or "default"),
            write_document=True,
        )
        outcome = create_skill_candidate(
            spec, credit=decision.to_dict(), dry_run=dry_run, run_async=candidate_records.run_async
        )
        status = outcome.get("status")
        if status == STATUS_NOT_WRITTEN:
            created.append(
                {
                    "kind": "recurring_intent",
                    "asset_id": asset_id,
                    "written": False,
                    "why_not": outcome.get("why_not") or {},
                }
            )
        elif status == STATUS_REJECTED:
            logger.info(
                "[evolution-loop] intent candidate rejected: %s %s",
                outcome.get("code"),
                outcome.get("violations") or "",
            )
        elif status == STATUS_DRY_RUN:
            created.append({"dry_run": True, "kind": "recurring_intent", "asset_id": asset_id})
        elif status == STATUS_CREATED:
            created.append(
                {
                    "candidate_id": outcome["candidate_id"],
                    "kind": "recurring_intent",
                    "asset_id": asset_id,
                    "written": True,
                    "attempts": outcome.get("attempts", 0),
                    "evidence_pack_id": outcome.get("pack_id", ""),
                }
            )
    return created


def procedures_for_cluster(
    cluster: Dict[str, Any], views: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """The procedural memories retrieved during this intent's episodes.

    These are what makes the resulting document a *compilation* of memory rather
    than a restatement of a tool list — and their refs are what gets demoted
    once the skill is live.
    """
    from core.evolution.generators.memory_scan import procedural_records, to_records
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

            raw = candidate_records.run_async(get_all_memories(user_id, top_k=500))
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
