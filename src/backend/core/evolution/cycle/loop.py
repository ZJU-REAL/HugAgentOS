"""One fleet-wide evolution pass: discover, attribute, promote, validate, persist."""
from __future__ import annotations
from core.evolution.cycle import ordering
from core.evolution.cycle import patterns
from core.evolution.cycle import promotion
from core.evolution.cycle import promotion_constants
import hashlib
from typing import Any, Dict, List
from core.evolution.evidence.credit import T_SKILL, aggregate_finding, assign_credit, confidence_from_evidence
from core.evolution.ir import RISK_HIGH, RISK_MEDIUM
import logging
from core.evolution.cycle import candidate_records
from core.evolution.cycle import episode_views
from core.evolution.cycle import graph_candidates

logger = logging.getLogger(__name__)

def run_evolution_cycle(*, limit: int = 500, dry_run: bool = False) -> Dict[str, Any]:
    """One fleet-wide pass: discover → attribute → promote → validate → persist.

    Returns a report rather than raising: this runs as a background job and a
    bad cycle must leave the product untouched.
    """
    if not candidate_records.cross_user_mining_available():
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
    from core.evolution.cycle.readiness import check_readiness

    readiness = check_readiness()
    if not readiness.ready:
        logger.warning("[evolution-loop] not ready, refusing to run: %s", readiness.blocking)
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

    views = episode_views.load_episode_views(limit=limit)
    report["episodes"] = len(views)
    if not views:
        return report

    discovered = patterns.discover_patterns(views)
    report["patterns"] = len(discovered)

    # Transferable ordering rules, extracted once across the whole corpus rather
    # than per pattern. A rigid sequence only helps tasks whose tool set it
    # happens to cover; the ordering rule behind it helps every task making the
    # same mistake, which is the difference between memorising and learning.
    constraints = ordering.extract_ordering_constraints(views)
    report["ordering_constraints"] = [c.to_dict() for c in constraints]

    from core.evolution.generators.skill_candidate_engine import STATUS_CREATED, STATUS_DRY_RUN, STATUS_REJECTED, SkillCandidateSpec, create_skill_candidate

    existing_signatures: Dict[str, str] = {}
    covered_tool_sets: List[List[str]] = []
    by_episode_id = {str(v.get("episode_id") or ""): v for v in views}

    for pattern in discovered:
        if pattern.kind == promotion_constants.PATTERN_SUCCESS_SUBSEQUENCE and pattern.tool_sequence:
            # An observation across many *successful* runs — aggregate decision,
            # not failure attribution with invented features.
            decision = aggregate_finding(
                target=T_SKILL,
                explanation=(
                    f"{pattern.support} 个成功 Episode 复现同一 "
                    f"{len(pattern.tool_sequence)} 步工具序列"
                    f"（成功率 {pattern.success_rate:.0%}）"
                ),
                confidence=confidence_from_evidence(pattern.support, saturates_at=10),
                evidence_count=pattern.support,
            )
        else:
            decision = assign_credit(candidate_records.features_for(pattern))
        if not decision.may_produce_candidate:
            # No-update / unidentifiable / a non-asset cause. Refusing here is
            # the mechanism that stops a KB gap being "fixed" by editing a skill.
            report["no_update"] += 1
            continue

        proposal = promotion.promote_tool_sequence_to_skill(
            pattern,
            episodes=views,
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
        sequence = [str(t) for t in proposal.payload.get("tool_sequence", [])]
        supporting = [
            by_episode_id[e]
            for e in (proposal.payload.get("episode_ids") or [])
            if e in by_episode_id
        ]

        # 规则门测统计，测不了语义——意图簇可能是巧合归并，序列可能是对任何
        # 任务都成立的通用组合。规则幸存者再过一道 LLM 裁决，拒绝即不产候选。
        from core.evolution.assessment.sop_judge import adjudicate_sequence_sop

        verdict = candidate_records.run_async(
            adjudicate_sequence_sop(
                representative=str(proposal.payload.get("intent") or ""),
                objectives=[str(v.get("objective") or "") for v in supporting],
                tool_sequence=sequence,
                occasions=int(proposal.payload.get("occasions") or 0),
                support=proposal.support,
                success_rate=pattern.success_rate,
            )
        )
        if not verdict.worthy:
            report["rejected"].append(
                {
                    "signature": proposal.signature,
                    "code": (
                        "sop_judge_unavailable"
                        if verdict.judge_unavailable
                        else "sop_judge_refused"
                    ),
                    "reason": verdict.reason,
                }
            )
            continue
        spec = SkillCandidateSpec(
            asset_id=asset_id,
            proposer="promotion_chain",
            hypothesis=proposal.rationale,
            views=supporting,
            tool_sequence=sequence,
            # Attach every rule whose tools this skill touches, and carry its
            # scope along with it. Attaching must not apply the usability
            # policy: doing so dropped exactly the context-dependent rules the
            # scoping exists to preserve — a rule contradicted in one task type
            # is still valid in the ones where it held, and that decision
            # belongs at use time, where the context is known.
            ordering_constraints=[
                c.to_dict()
                for c in constraints
                if c.before in sequence and c.after in sequence
            ],
            operation="patch" if proposal.patch_target else "new",
            base_version="current" if proposal.patch_target else "v0",
            # A brand-new capability is riskier than tweaking an existing one.
            risk_tier=RISK_MEDIUM if proposal.patch_target else RISK_HIGH,
            scope={"level": "workspace"},
            tenant_id=str(
                (supporting[0].get("tenant_id") if supporting else "") or "default"
            ),
            write_document=False,
        )
        # 裁决结论随候选进 credit_decision 落库，评审员能看到机器放行的理由。
        outcome = create_skill_candidate(
            spec,
            credit={**decision.to_dict(), "sop_judge": verdict.to_dict()},
            dry_run=dry_run,
        )
        status = outcome.get("status")
        if status == STATUS_REJECTED:
            report["rejected"].append(
                {"signature": proposal.signature, "code": outcome.get("code")}
            )
        elif status == STATUS_DRY_RUN:
            report["candidates"].append({"dry_run": True, "signature": proposal.signature})
        elif status == STATUS_CREATED:
            covered_tool_sets.append(sequence)
            report["candidates"].append(
                {
                    "candidate_id": outcome["candidate_id"],
                    "signature": proposal.signature,
                    "evidence_pack_id": outcome.get("pack_id", ""),
                }
            )

    # ── Ordering rules that no sequence candidate carries ────────────────────
    #
    # Without this the only shape of learning that can become an asset is "five
    # runs repeated one identical tool sequence", which on real traffic is close
    # to unobservable — measured on this deployment's history, every episode's
    # tool sequence was unique. The rules are the part that generalises, so they
    # get to stand on their own rather than only riding along on a sequence that
    # happens to qualify.
    orphan_rules = candidate_records.uncovered_constraints(constraints, covered_tool_sets)
    if orphan_rules:
        rule_decision = assign_credit(candidate_records.features_for_ordering_rules(orphan_rules))
        if not rule_decision.may_produce_candidate:
            report["no_update"] += 1
        else:
            rule_payload = [c.to_dict() for c in orphan_rules]
            signature = "|".join(sorted(f"{c.before}<{c.after}" for c in orphan_rules))
            asset_id = "auto-rules-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
            supporting_views = [
                view
                for view in views
                if view.get("verdict") == "success"
                and any(
                    c.before in (view.get("tool_sequence") or [])
                    and c.after in (view.get("tool_sequence") or [])
                    for c in orphan_rules
                )
            ]
            rule_spec = SkillCandidateSpec(
                asset_id=asset_id,
                proposer="promotion_chain",
                hypothesis=(
                    f"{len(orphan_rules)} 条工具顺序约束在 {len(supporting_views)} 条成功执行中"
                    "稳定成立且跨多种工具组合复现，但规划阶段每次仍可能排错"
                ),
                views=supporting_views,
                # Deliberately no tool sequence: this asset claims no fixed
                # order of its own, only constraints. Materialisation renders
                # it as rules.
                tool_sequence=[],
                ordering_constraints=rule_payload,
                operation="new",
                base_version="v0",
                risk_tier=RISK_HIGH,
                scope={"level": "workspace"},
                tenant_id=str(
                    (supporting_views[0].get("tenant_id") if supporting_views else "")
                    or "default"
                ),
                write_document=False,
            )
            outcome = create_skill_candidate(
                rule_spec, credit=rule_decision.to_dict(), dry_run=dry_run
            )
            status = outcome.get("status")
            if status == STATUS_REJECTED:
                report["rejected"].append({"signature": signature, "code": outcome.get("code")})
            else:
                report["proposals"] += 1
                if status == STATUS_DRY_RUN:
                    report["candidates"].append(
                        {"dry_run": True, "signature": signature, "kind": "ordering_rules"}
                    )
                elif status == STATUS_CREATED:
                    report["candidates"].append(
                        {
                            "candidate_id": outcome["candidate_id"],
                            "signature": signature,
                            "kind": "ordering_rules",
                            "evidence_pack_id": outcome.get("pack_id", ""),
                        }
                    )

    # The other two engines, plus orchestration. Every finding below becomes a
    # candidate in the same table, behind the same security gate and the same
    # review queue as the skill candidates above — a finding that stops at a
    # report is a log line, not an engine.
    report.update(graph_candidates.run_other_engines(views, dry_run=dry_run))

    logger.info(
        "[evolution-loop] cycle done: episodes=%d patterns=%d candidates=%d no_update=%d",
        report["episodes"],
        report["patterns"],
        len(report["candidates"]),
        report["no_update"],
    )
    return report
