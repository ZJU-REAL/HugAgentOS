"""Ramp, roll back and retire skill releases."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from core.db.engine import SessionLocal
import logging
from core.evolution.cycle import activation_policy
from core.evolution.cycle import activation_types

logger = logging.getLogger(__name__)

def advance_skill_release(
    release_id: str,
    *,
    actor: str,
    observed_metrics: Optional[Dict[str, float]] = None,
    guardrail_thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Ramp a release one step: shadow → canary 5 → 25 → 100 → active.

    One step per call, deliberately. A single "go live" button that walked the
    whole ladder would reproduce the behaviour this replaces — the point of the
    ladder is that somebody looks at the metrics between rungs.

    Guardrails are checked when metrics are supplied, and a breach rolls back
    rather than pausing: the recorded threshold already *is* the level at which
    the release is unacceptable.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease
    from core.evolution.cycle.release import ACTIVE, CANARY, CANARY_STEPS, SHADOW, can_promote, evaluate_guardrails, next_traffic, should_auto_rollback

    if not actor:
        raise activation_types.ActivationError("actor_required", "推进发布必须署名")

    with SessionLocal() as db:
        release = db.get(EvolutionRelease, release_id)
        if release is None:
            raise activation_types.ActivationError("release_not_found", f"发布记录不存在: {release_id}")
        if activation_policy.is_frozen(db):
            raise activation_types.ActivationError("globally_frozen", "全局冻结开关已打开，禁止推进发布")

        candidate = db.get(EvolutionCandidate, release.candidate_id)
        risk = str(getattr(candidate, "risk_tier", "") or "low")
        guardrails = dict(release.guardrails or {})
        completed: List[str] = list(guardrails.get("completed_verification") or [])
        proposer = str(guardrails.get("proposer") or getattr(candidate, "proposer", "") or "")

        if observed_metrics:
            thresholds = guardrail_thresholds or guardrails.get("thresholds") or {}
            breaches = evaluate_guardrails(observed_metrics, thresholds, window="canary")
            if should_auto_rollback(breaches):
                detail = ", ".join(
                    f"{b.metric}={b.observed}>{b.threshold}" for b in breaches
                )
                db.commit()
                rollback_skill_activation(
                    release_id,
                    actor=actor,
                    reason=f"guardrail breach: {detail}",
                    kind="guardrail",
                )
                return {
                    "release_id": release_id,
                    "stage": "rolled_back",
                    "guardrail_breaches": [b.to_dict() for b in breaches],
                }

        stage = str(release.stage or "")
        traffic = int(release.traffic_percent or 0)

        if stage == SHADOW:
            target_stage, target_traffic = CANARY, CANARY_STEPS[0]
            completed = sorted(set(completed) | {"shadow"})
        elif stage == CANARY and traffic < CANARY_STEPS[-1]:
            target_stage, target_traffic = CANARY, next_traffic(traffic)
        elif stage == CANARY:
            target_stage, target_traffic = ACTIVE, 100
            completed = sorted(set(completed) | {"canary"})
        else:
            raise activation_types.ActivationError(
                "not_rampable", f"当前阶段 {stage} 无法继续放量"
            )

        allowed, reason = can_promote(
            current_stage=stage,
            target_stage=target_stage,
            risk_tier=risk,
            # ``human_approval`` is this call: a named actor who is not the
            # proposer. Recording it before the check keeps the requirement
            # satisfied by the act itself rather than by a stored flag.
            completed_verification=sorted(set(completed) | {"human_approval"})
            if target_stage == ACTIVE
            else completed,
            proposer=proposer,
            approver=actor,
        )
        # A same-stage traffic bump is not a transition; can_promote would reject
        # canary→canary, which is correct for stages and wrong for ramp steps.
        if not allowed and not (stage == target_stage == CANARY):
            raise activation_types.ActivationError("promotion_refused", reason)

        if target_stage == ACTIVE:
            # 冲突的 L3 证据在 shadow/canary 里观测没问题，但升到全量必须先由
            # 人裁决冲突本身——适用条件不可信的技能不配自动放大到所有人。
            if activation_policy.has_graph_conflict(candidate):
                raise activation_types.ActivationError(
                    "graph_conflict_requires_review",
                    "候选的 L3 图谱证据存在未裁决的冲突，禁止放量到 active",
                )
            activation_policy.check_pace(db, tenant_id=str((release.scope_filter or {}).get("tenant_id") or "default"))

        release.stage = target_stage
        release.traffic_percent = target_traffic
        guardrails["completed_verification"] = completed
        if target_stage == ACTIVE:
            guardrails["approved_by"] = actor
        release.guardrails = guardrails
        release.approved_by = actor
        if candidate is not None and target_stage == ACTIVE:
            candidate.status = ACTIVE
        db.commit()

        logger.info(
            "[activation] release %s → %s/%d%% by %s",
            release_id,
            target_stage,
            target_traffic,
            actor,
        )
        return {
            "release_id": release_id,
            "stage": target_stage,
            "traffic_percent": target_traffic,
            "completed_verification": completed,
            "exposed_to": (
                "all users in scope"
                if target_stage == ACTIVE
                else f"{target_traffic}% of users in scope"
            ),
        }


def rollback_skill_activation(
    release_id: str, *, actor: str, reason: str, kind: str = "manual"
) -> Dict[str, Any]:
    """Undo a materialisation, restoring the snapshot taken at activation time.

    With no prior version the skill is disabled rather than deleted: history
    referencing it must stay resolvable, and a disabled row is both reversible
    and visible in a way a missing row is not.

    ``kind`` separates "a guardrail caught it" from "a person caught it".
    Collapsing the two would hide how well the evaluation stage is working,
    which is the number that tells you whether the ramp is worth its cost.
    """
    from core.db.models import AdminSkill
    from core.db.models.evolution import (
        EvolutionAgentProfile,
        EvolutionCandidate,
        EvolutionRelease,
    )
    from core.evolution.cycle.release import ROLLBACK_GUARDRAIL, ROLLBACK_MANUAL, ROLLED_BACK

    if not actor or not reason:
        raise activation_types.ActivationError("audit_required", "回滚必须署名并填写理由")

    with SessionLocal() as db:
        release = db.get(EvolutionRelease, release_id)
        if release is None:
            raise activation_types.ActivationError("release_not_found", f"发布记录不存在: {release_id}")

        previous = (release.guardrails or {}).get("previous")
        skill_id = release.target_asset_id
        row = (
            db.query(AdminSkill)
            .filter(AdminSkill.skill_id == skill_id)
            .first()
        )
        invalidated: List[str] = []
        if row is not None:
            if previous:
                row.skill_content = previous["skill_content"]
                row.display_name = previous["display_name"]
                row.description = previous["description"]
                row.version = previous["version"]
                row.tags = previous["tags"]
                row.allowed_tools = previous["allowed_tools"]
                row.extra_files = previous["extra_files"]
                row.is_enabled = previous["is_enabled"]
            else:
                row.is_enabled = False
                # No prior version means the skill just stopped being loadable,
                # so a profile assembled on top of it is now promising a
                # capability that fails silently — same cross-layer rule as
                # retirement. With a restored prior version the skill stays
                # loadable and the profiles stay valid.
                for profile_row in (
                    db.query(EvolutionAgentProfile)
                    .filter(EvolutionAgentProfile.is_active.is_(True))
                    .all()
                ):
                    payload = profile_row.payload or {}
                    if skill_id in (payload.get("skill_ids") or []):
                        profile_row.is_active = False
                        invalidated.append(profile_row.profile_id)

        release.stage = ROLLED_BACK
        release.rolled_back_at = datetime.now(timezone.utc)
        release.rollback_reason = reason
        release.rollback_kind = (
            ROLLBACK_GUARDRAIL if kind == "guardrail" else ROLLBACK_MANUAL
        )

        candidate = db.get(EvolutionCandidate, release.candidate_id)
        if candidate is not None:
            candidate.status = ROLLED_BACK
        candidate_id = release.candidate_id
        db.commit()

    # The reverse edge of the promotion chain. Materialisation down-weighted the
    # memories this skill was compiled from; rolling the skill back must restore
    # them in the same motion, or the knowledge stays half-priced with no
    # carrier — retrieval skips it because a skill supposedly covers it, and the
    # skill is gone. Same ledger the retire path already replays.
    from core.evolution.generators.memory_apply import revert_memory_ops

    restored = revert_memory_ops(candidate_id=candidate_id) if candidate_id else {}

    # 血缘：这一版回滚到了哪一版。审计问「技能 v2 后来怎么了」时，答案在链上。
    try:
        from core.evolution.evidence import lineage

        current_version = str((previous or {}).get("version") or "")
        lineage.record_links(
            [
                (
                    "skill",
                    f"{skill_id}@rolled_back",
                    lineage.REL_ROLLED_BACK_TO,
                    "skill",
                    f"{skill_id}@{current_version or 'disabled'}",
                )
            ],
            candidate_id=candidate_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[activation] rollback lineage skipped: %s", exc)

    logger.info(
        "[activation] rolled back %s (release=%s memories restored=%d profiles invalidated=%s)",
        skill_id,
        release_id,
        len(restored.get("reverted", [])),
        invalidated,
    )
    return {
        "release_id": release_id,
        "restored_previous": bool(previous),
        "restored_memory_ops": restored.get("reverted", []),
        "invalidated_profiles": invalidated,
        "reason": reason,
    }


def retire_skill_candidate(candidate_id: str, *, approver: str) -> Dict[str, Any]:
    """Take an approved retirement into effect.

    Retirement is disabling plus a release row, never a delete. Three reasons it
    is done this way: the evidence behind the decision stays inspectable, the
    skill can be brought back in one operation if the decision was wrong, and
    anything built on top of it — a profile naming it, a sub-agent requiring it
    — can be found and invalidated rather than silently pointing at nothing.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease

    from core.db.models import AdminSkill
    from core.db.models.evolution import EvolutionAgentProfile
    from core.evolution.cycle.release import RETIRED, new_release_id

    if not approver:
        raise activation_types.ActivationError("approver_required", "退役必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise activation_types.ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.operation != "deprecate":
            raise activation_types.ActivationError("not_a_retirement", "该候选不是退役候选")
        if approver == (candidate.proposer or ""):
            raise activation_types.ActivationError("self_approval_forbidden", "生成者不能批准自己提出的退役")

        skill_id = candidate.target_asset_id
        row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        if row is None:
            raise activation_types.ActivationError("skill_not_found", f"技能不存在: {skill_id}")

        previous_enabled = bool(row.is_enabled)
        row.is_enabled = False

        # Cross-layer: anything assembled on top of this skill stops being
        # applied. Leaving them active would leave a profile promising a
        # capability that is no longer loadable, which fails silently.
        invalidated: List[str] = []
        for profile_row in (
            db.query(EvolutionAgentProfile)
            .filter(EvolutionAgentProfile.is_active.is_(True))
            .all()
        ):
            payload = profile_row.payload or {}
            if skill_id in (payload.get("skill_ids") or []):
                profile_row.is_active = False
                invalidated.append(profile_row.profile_id)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind="skill",
            target_asset_id=skill_id,
            stage=RETIRED,
            traffic_percent=0,
            scope_filter={},
            guardrails={
                "previous_enabled": previous_enabled,
                "invalidated_profiles": invalidated,
                "proposer": candidate.proposer or "",
            },
            rollback_version_id=row.version or "",
            approved_by=approver,
        )
        db.add(release)

        candidate.status = "active"
        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        # Restore the memories this skill was compiled from: the knowledge lost
        # its cheaper carrier, so it goes back to being paid for by retrieval.
        from core.evolution.generators.memory_apply import revert_memory_ops

        source_candidate = activation_policy.source_candidate_for(db, skill_id)
        restored = revert_memory_ops(candidate_id=source_candidate) if source_candidate else {}

        logger.info(
            "[activation] retired %s (profiles invalidated=%s memories restored=%d)",
            skill_id,
            invalidated,
            len(restored.get("reverted", [])),
        )
        return {
            "skill_id": skill_id,
            "release_id": release.release_id,
            "invalidated_profiles": invalidated,
            "restored_memory_ops": restored.get("reverted", []),
        }
