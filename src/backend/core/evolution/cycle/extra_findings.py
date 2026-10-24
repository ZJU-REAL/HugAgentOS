"""Intent, decremental and negative-transfer findings."""
from __future__ import annotations
from core.evolution.cycle import promotion
from core.evolution.cycle import retirement
import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple
from core.evolution.cycle import cycle_extras_constants
from core.evolution.cycle import usage_evidence

def asset_id(prefix: str, seed: str) -> str:
    return f"{prefix}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def intent_findings(views: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Recurring requests whose handling never settled.

    The pairing matters. Recurrence alone says the question is common;
    recurrence **with plan variety** says the system has not found a stable way
    to answer it and is paying the planning cost fresh every time. That second
    condition is what distinguishes a capability gap from a popular question.
    """
    from core.evolution.assessment.similarity import cluster_intents

    clusters, method = cluster_intents(views, min_support=cycle_extras_constants.MIN_INTENT_OCCASIONS)
    findings: List[Dict[str, Any]] = []
    for cluster in clusters:
        if cluster.occasions < cycle_extras_constants.MIN_INTENT_OCCASIONS:
            # Recurrence inside one conversation is a user rephrasing after a
            # bad answer, which is evidence about that answer, not about demand.
            continue
        if cluster.plan_variety < cycle_extras_constants.MIN_PLAN_VARIETY:
            # Same question, same plan, every time. Already effectively a skill;
            # writing one down adds an asset to maintain and changes nothing.
            continue
        if cluster.success_rate < cycle_extras_constants.INTENT_MIN_SUCCESS_RATE:
            # Recurring *and* failing. Distilling the current plan would encode
            # the failure. This belongs to attribution, not to promotion.
            findings.append(
                {
                    "kind": "recurring_failure_intent",
                    "cluster": cluster.to_dict(),
                    "action": "no_skill_candidate",
                    "reason": (
                        f"「{cluster.representative[:30]}」类请求出现 {cluster.occasions} 次，"
                        f"但成功率只有 {cluster.success_rate:.0%}——"
                        "把当前做法蒸馏出来等于把失败固化"
                    ),
                }
            )
            continue
        findings.append(
            {
                "kind": "recurring_intent",
                "cluster": cluster.to_dict(),
                "action": "skill_candidate",
                "asset_id": asset_id("auto-intent", cluster.representative),
                "reason": (
                    f"同一意图在 {cluster.occasions} 次独立会话中出现，却用了 "
                    f"{cluster.plan_variety} 种不同的工具序列——每次都在重新规划"
                ),
            }
        )
    return {"method": method, "clusters": len(clusters), "findings": findings}


def decremental_findings(
    views: Sequence[Dict[str, Any]],
    *,
    skill_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    declared_tools: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Merge, retirement and negative-transfer findings.

    ``skill_stats`` is derived from the episode views when not supplied.
    ``declared_tools`` maps skill id → the tools its own document names; overlap
    is computed from that and never from the tools observed in episodes where
    the skill happened to be loaded, which would pair every co-enabled skill.
    """

    stats = skill_stats if skill_stats is not None else usage_evidence.skill_usage_from_views(views)

    overlaps: List[Tuple[str, str, int]] = []
    if declared_tools is None:
        declared_tools = usage_evidence.declared_tools_for(sorted(stats))
    if declared_tools:
        overlaps = promotion.find_overlaps(declared_tools)

    retirements = retirement.find_retirement_candidates(stats)
    return {
        "overlaps": [
            {"a": a, "b": b, "shared_tools": shared} for a, b, shared in overlaps
        ],
        "retirements": retirements,
        "asset_count": len(stats),
        "negative_transfer": negative_transfer_for_activations(views),
    }


def negative_transfer_for_activations(
    views: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Check each recent activation for collateral damage.

    The detector needs a corpus split into before and after *something*. Calling
    it on views where nothing carries that marker returns nothing on every
    corpus, which is a detector that runs and cannot find anything — the exact
    shape of defect this rework exists to remove. So the split is computed here,
    from the release ledger, once per activated asset.
    """
    activations = usage_evidence.recent_activations()
    if not activations:
        return []

    findings: List[Dict[str, Any]] = []
    for asset_id, activated_at, related in activations:
        split = [
            {**view, "after": usage_evidence.is_after(view.get("created_at"), activated_at)}
            for view in views
            if view.get("created_at") is not None
        ]
        if not any(v["after"] for v in split) or not any(not v["after"] for v in split):
            # One-sided evidence cannot show a change. Reporting nothing is the
            # honest output; reporting a drop from an empty baseline is not.
            continue
        findings.extend(
            negative_transfer_findings(split, asset_id=asset_id, related_task_types=related)
        )
    return findings


def negative_transfer_findings(
    views: Sequence[Dict[str, Any]],
    *,
    asset_id: str = "",
    related_task_types: Sequence[str] = (),
    before_key: str = "before",
    after_key: str = "after",
) -> List[Dict[str, Any]]:
    """Task types that got worse after some activation, unrelated to it.

    The one failure mode of joint evolution that no single release's guardrail
    can catch: each change looks fine on the metric it was measured against, and
    the damage shows up somewhere nobody was looking.
    """

    by_type_before: Dict[str, List[bool]] = {}
    by_type_after: Dict[str, List[bool]] = {}
    for view in views:
        target = by_type_after if view.get(after_key) else by_type_before
        target.setdefault(str(view.get("task_type") or "chat"), []).append(
            view.get("verdict") == "success"
        )

    def rates(source: Dict[str, List[bool]]) -> Dict[str, float]:
        return {k: (sum(v) / len(v) if v else 0.0) for k, v in source.items()}

    findings = retirement.detect_negative_transfer(
        asset_id=asset_id or "unspecified",
        # Only *unrelated* task types are examined. A dip on the task the change
        # targeted is an ordinary regression its own guardrail catches; the
        # dangerous case is collateral damage where nobody is looking.
        related_task_types=list(related_task_types),
        before=rates(by_type_before),
        after=rates(by_type_after),
    )
    return [f.to_dict() for f in findings]
