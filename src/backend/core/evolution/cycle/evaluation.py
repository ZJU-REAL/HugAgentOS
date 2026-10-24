"""Replay-evaluate candidates and record the results."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from core.db.engine import SessionLocal
from core.db.models.evolution import EvolutionCandidate
import logging
logger = logging.getLogger(__name__)

def record_evaluation(candidate_id: str, report: Any, eval_type: str) -> None:
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


def replay_coverage_for(candidate_id: str, task_ids: List[str]) -> Optional[Dict[str, Any]]:
    """Coverage assessment for candidates that carry an evidence pack.

    Returns ``None`` for pre-pack candidates — the gate governs only what the
    unified engine produced, so legacy candidates keep their old semantics.
    """
    try:
        from core.evolution.evidence import lineage
        from core.evolution.assessment.applicability import assess_replay_coverage

        with SessionLocal() as db:
            candidate = db.get(EvolutionCandidate, candidate_id)
            pack_id = str(getattr(candidate, "evidence_pack_id", "") or "")
        if not pack_id:
            return None
        pack = lineage.load_evidence_pack(pack_id)
        if pack is None:
            return None
        return assess_replay_coverage(pack, task_ids)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[evolution-loop] replay coverage check skipped: %s", exc)
        return None


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
    from core.evolution.cycle.release import DRAFT, REJECTED, REPLAY_PASSED, is_legal_transition
    from core.evolution.evidence.replay import VERDICT_IMPROVED, VERDICT_REGRESSED, run_tiered_replay

    tiered = run_tiered_replay(task_ids, run_arm)
    report = tiered.report or tiered.sentinel_report
    if report is None:
        return None

    record_evaluation(candidate_id, report, "replay" if tiered.passed_sentinel else "sentinel")

    # Replay 覆盖闸门：带证据 pack 的候选，其回放集必须覆盖 pack 声称的行为面
    # （成功 / 失败恢复 / 负样本 / 图谱依赖两类场景）。只用成功案例喂出来的
    # "improved" 是单面证据，不允许换来 replay_passed；回归照常生效——安全
    # 方向的结论不需要覆盖完整也成立。
    coverage: Optional[Dict[str, Any]] = None
    if report.verdict == VERDICT_IMPROVED:
        coverage = replay_coverage_for(candidate_id, task_ids)
        if coverage is not None and not coverage.get("covered", True):
            return {
                "candidate_id": candidate_id,
                "verdict": report.verdict,
                "status": DRAFT,
                "transition": "held_insufficient_coverage",
                "coverage": coverage,
                "tasks_run": tiered.tasks_run,
                "effect_size": report.effect_size,
                "p_value": report.p_value,
            }

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
