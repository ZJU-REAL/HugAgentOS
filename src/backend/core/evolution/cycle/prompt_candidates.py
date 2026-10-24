"""Prompt-fragment candidates for failures attributed to the prompt."""
from __future__ import annotations
import hashlib
from typing import Any, Dict, List
from core.evolution.evidence.credit import CreditFeatures, assign_credit
from core.evolution.ir import RISK_HIGH, EvolutionIR, InvariantViolation, Rollback, validate_ir
import logging
from core.evolution.cycle import candidate_records

logger = logging.getLogger(__name__)

def prompt_candidates(views: List[Dict[str, Any]], *, dry_run: bool) -> List[Dict[str, Any]]:
    """Prompt fragments, for the failures attribution already blames on the prompt."""
    from core.evolution.generators.prompt_gen import generate_prompt_candidate

    payload, evidence, draft = candidate_records.run_async(
        generate_prompt_candidate(
            views,
            cause="归因判定该失败源于提示词未声明相关约束",
            existing_fragments=active_prompt_fragments(),
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

    asset_id = "auto-prompt-" + hashlib.sha256(payload["fragment"].encode("utf-8")).hexdigest()[:12]
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
    candidate_id = candidate_records.persist_candidate(
        ir,
        credit=decision.to_dict(),
        proposer="prompt_engine",
        evidence_refs=evidence.episode_ids[:20],
    )
    return (
        [{"candidate_id": candidate_id, "kind": "prompt", "asset_id": asset_id}]
        if candidate_id
        else []
    )


def active_prompt_fragments() -> List[str]:
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
