"""L3 graph evolution candidates and the dispatcher for the other engines."""
from __future__ import annotations
import hashlib
from typing import Any, Dict, List
from core.evolution.evidence.credit import T_MEMORY, aggregate_finding, confidence_from_evidence
from core.evolution.ir import RISK_MEDIUM, EvolutionIR, InvariantViolation, Rollback, validate_ir
import logging
from core.evolution.cycle import candidate_records
from core.evolution.cycle import composition_candidates
from core.evolution.cycle import memory_candidates
from core.evolution.cycle import prompt_candidates

logger = logging.getLogger(__name__)

def graph_evolution_candidates(
    views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """L3 的演进：强化/降权自动执行，语义变更走人工候选。

    Per user, because the graph is per user scope: a contradiction between two
    people's graphs is not a contradiction at all.
    """
    from core.config.settings import settings

    if not (settings.memory.enabled and settings.memory.graph_enabled):
        return []

    from core.evolution.generators.graph_scan import apply_graph_ops, scan_graph_relations

    observed_by_user: Dict[str, Dict[str, int]] = {}
    for view in views:
        user_id = str(view.get("user_id") or "")
        if not user_id:
            continue
        bucket = observed_by_user.setdefault(user_id, {})
        for relation in view.get("graph_relations") or []:
            relation_id = str(relation.get("relation_id") or "")
            if relation_id:
                bucket[relation_id] = bucket.get(relation_id, 0) + 1

    created: List[Dict[str, Any]] = []
    for user_id, observed in observed_by_user.items():
        try:
            from core.memory.graph import list_graph_relations

            relations = candidate_records.run_async(list_graph_relations(user_id, limit=200))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[evolution-loop] graph listing failed for %s: %s", user_id, exc)
            continue
        if not relations:
            continue

        ops = scan_graph_relations(relations, observed_relation_ids=observed)
        if not ops:
            continue
        if dry_run:
            created.append(
                {"dry_run": True, "kind": "graph", "user_id": user_id, "ops": len(ops)}
            )
            continue

        outcome = apply_graph_ops(ops, scope_user_id=user_id)
        manual = outcome.get("manual") or []
        entry: Dict[str, Any] = {
            "kind": "graph",
            "user_id": user_id,
            "auto_applied": len(outcome.get("applied") or []),
            "manual_proposed": len(manual),
        }
        if manual:
            decision = aggregate_finding(
                target=T_MEMORY,
                explanation="；".join(str(op.get("reason") or "") for op in manual[:3]),
                confidence=confidence_from_evidence(len(manual), saturates_at=6),
                evidence_count=len(manual),
            )
            if decision.may_produce_candidate:
                asset_id = "graph-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
                ir = EvolutionIR(
                    target_kind="memory",
                    target_asset_id=asset_id,
                    base_version="current",
                    operation="update",
                    hypothesis=(
                        f"图谱有 {len(manual)} 处语义变更需要人工裁决："
                        + "；".join(str(op.get("reason") or "") for op in manual[:3])
                    ),
                    changes=[{"user_id": user_id, "graph_ops": manual}],
                    evidence_refs=[
                        str(op.get("relation_id") or "") for op in manual if op.get("relation_id")
                    ][:20],
                    risk_tier=RISK_MEDIUM,
                    scope={"level": "user", "user_id": user_id},
                    rollback=Rollback(version_id="current", triggers=["manual"]),
                    evaluation_plan={"replay_set": "personal", "primary_metric": "task_success"},
                )
                try:
                    validate_ir(
                        ir,
                        credit_decision=decision.to_dict(),
                        allowed_scopes=["user", "workspace"],
                    )
                except InvariantViolation as violation:
                    logger.info(
                        "[evolution-loop] graph candidate rejected: %s", violation.code
                    )
                else:
                    candidate_id = candidate_records.persist_candidate(
                        ir,
                        credit=decision.to_dict(),
                        proposer="graph_engine",
                        evidence_refs=list(ir.evidence_refs),
                    )
                    if candidate_id:
                        entry["candidate_id"] = candidate_id
        created.append(entry)
    return created


def run_other_engines(views: List[Dict[str, Any]], *, dry_run: bool) -> Dict[str, Any]:
    """Memory, decremental, intent, orchestration, prompt and graph — each with an exit.

    Independently guarded: a failure in one engine must not cost the others
    their findings, because they answer different questions and an operator
    needs whichever ones succeeded.
    """
    from core.evolution.cycle.cycle_extras import run_extras

    scan_findings = memory_candidates.memory_scan_findings(views)
    extras = run_extras(views, scan_findings=scan_findings)

    produced: List[Dict[str, Any]] = []
    for name, fn in (
        ("memory", lambda: memory_candidates.memory_candidates(extras, views, dry_run=dry_run)),
        ("intent", lambda: memory_candidates.intent_skill_candidates(extras, views, dry_run=dry_run)),
        ("decremental", lambda: composition_candidates.decremental_candidates(extras, dry_run=dry_run)),
        ("orchestration", lambda: composition_candidates.orchestration_candidates(views, dry_run=dry_run)),
        ("prompt", lambda: prompt_candidates.prompt_candidates(views, dry_run=dry_run)),
        ("graph", lambda: graph_evolution_candidates(views, dry_run=dry_run)),
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
