"""Retirement, merge, profile and sub-agent candidates."""
from __future__ import annotations
from typing import Any, Dict, List
from core.evolution.evidence.credit import T_ORCHESTRATION, T_SKILL, aggregate_finding, confidence_from_evidence
from core.evolution.ir import RISK_HIGH, RISK_MEDIUM, EvolutionIR, InvariantViolation, Rollback, validate_ir
import logging
from core.evolution.cycle import candidate_records

logger = logging.getLogger(__name__)

def decremental_candidates(extras: Dict[str, Any], *, dry_run: bool) -> List[Dict[str, Any]]:
    """Retirement and merge findings, as candidates rather than as a list.

    Retiring goes through the same approval path as creating. Removing a
    capability produces no error — only a slow decline — so it is precisely the
    direction that must not be automatic.
    """
    from core.evolution.cycle.promotion_constants import ACTION_REWRITE

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
            confidence=confidence_from_evidence(int(item.get("offers", 0) or 0), saturates_at=20),
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
            changes=[
                {
                    "retire_reason": item.get("reason"),
                    "stats": {
                        k: v for k, v in item.items() if k not in ("asset_id", "explanation")
                    },
                }
            ],
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
        candidate_id = candidate_records.persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="decremental_engine",
            evidence_refs=[asset_id],
        )
        if candidate_id:
            created.append(
                {
                    "candidate_id": candidate_id,
                    "kind": "retirement",
                    "asset_id": asset_id,
                    "reason": item.get("reason"),
                }
            )
    return created


def orchestration_candidates(
    views: List[Dict[str, Any]], *, dry_run: bool
) -> List[Dict[str, Any]]:
    """Profile and sub-agent candidates.

    The tool authority every candidate here is measured against is the *current*
    grant, read once. A profile may only ever narrow it, and a derived sub-agent
    may only ever receive a subset — both enforced by the IR gate rather than by
    the generator, so the check holds even for a candidate that arrived by some
    other route.
    """
    from core.capabilities.catalog.api import get_enabled_ids
    from core.evolution.profiles.agent_profile_constants import TARGET_KIND
    from core.evolution.profiles.agent_profile import builtin_profile, validate_profile
    from core.evolution.generators.orchestration_gen import generate_orchestration_proposals, profile_from_proposals

    base_tools = [str(t) for t in get_enabled_ids("tools")] + [
        str(t) for t in get_enabled_ids("mcp")
    ]
    offered_tools = {str(view.get("task_type") or "chat"): base_tools for view in views}
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
        candidate_id = candidate_records.persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="orchestration_engine",
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
        candidate_id = candidate_records.persist_candidate(
            ir,
            credit=decision.to_dict(),
            proposer="orchestration_engine",
            evidence_refs=subagent.evidence_refs,
        )
        if candidate_id:
            created.append(
                {"candidate_id": candidate_id, "kind": "subagent", "name": subagent.name}
            )
    return created
