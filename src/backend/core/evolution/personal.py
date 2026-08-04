"""Per-user evolution — the path that works in every edition.

Cross-user distillation already exists in this product (the nightly skill
distiller), so the value this adds is the *intra-user* loop: a person's own
recurring patterns become a capability that person can accept, without any of
their evidence leaving their own scope.

That framing settles the edition question rather than working around it.
Personal evolution needs no cross-user aggregation, so it runs in the community
edition unchanged; what is enterprise-only is the fleet-wide path — mining
across users and releasing to everyone — which is a governance surface, not a
feature toggle.

Two properties follow and are enforced here rather than assumed:

* **Evidence never leaves the user.**  Mining is filtered by ``user_id`` at the
  query, so a personal cycle cannot see another person's episodes even by
  accident.
* **The result is private.**  Candidates produced here are approved by their
  owner and installed as their own skill; no personal cycle can change what
  anyone else's agent does.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal
from core.evolution import promotion as P
from core.evolution.credit import T_SKILL, aggregate_finding, confidence_from_evidence
from core.evolution.ir import (
    RISK_LOW,
    RISK_MEDIUM,
    EvolutionIR,
    InvariantViolation,
    Rollback,
    validate_ir,
)

logger = logging.getLogger(__name__)

# A personal pattern needs less corroboration than a fleet-wide one: the blast
# radius is one person, and requiring the same evidence as a global capability
# would mean personal evolution effectively never fires.
PERSONAL_MIN_SUPPORT = 3


def load_personal_episodes(user_id: str, *, limit: int = 300) -> List[Dict[str, Any]]:
    """This user's episodes, flattened for pattern mining.

    Filtered on ``user_id`` in the query — not after the fact — so a personal
    cycle structurally cannot read someone else's evidence.
    """
    from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent

    if not user_id:
        return []

    views: List[Dict[str, Any]] = []
    try:
        with SessionLocal() as db:
            episodes = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.user_id == user_id)
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
            by_episode: Dict[str, List[Any]] = {}
            for event in events:
                by_episode.setdefault(event.episode_id, []).append(event)

            # One flattening routine, shared with the fleet cycle. Two copies of
            # this drifted apart once already: the personal path kept reading
            # raw content hashes as memory refs, which the overlay cannot key
            # on, so a personal memory candidate could never have been applied.
            from core.evolution.loop import _view_for

            for episode in episodes:
                views.append(_view_for(episode, by_episode.get(episode.episode_id, [])))
    except Exception as exc:
        logger.warning("[personal-evo] loading episodes failed for %s: %s", user_id, exc)
        return []
    return views


def _discover(views: List[Dict[str, Any]]) -> List[P.Pattern]:
    """Pattern discovery at the personal support threshold."""
    original = P.MIN_SUPPORT
    try:
        P.MIN_SUPPORT = PERSONAL_MIN_SUPPORT
        return P.discover_patterns(views)
    finally:
        P.MIN_SUPPORT = original


def run_personal_cycle(
    user_id: str,
    *,
    limit: int = 300,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """One evolution pass over a single user's evidence.

    Returns a report rather than raising — this runs in the background and a bad
    cycle must leave the product untouched.
    """
    from core.evolution.prefs import resolve_prefs
    from core.evolution.loop import _persist_candidate

    report: Dict[str, Any] = {
        "user_id": user_id,
        "episodes": 0,
        "patterns": 0,
        "candidates": [],
        "rejected": [],
        "no_update": 0,
        "scope": "personal",
    }

    from core.evolution.readiness import check_readiness

    readiness = check_readiness()
    report["readiness"] = readiness.to_dict()
    if not readiness.ready:
        # Same refusal as the fleet cycle, and for the same reason: a personal
        # cycle that runs without semantics finds nothing and says nothing about
        # why.
        report["skipped"] = "not_ready"
        return report

    prefs = resolve_prefs(user_id)
    if not prefs.get("enabled", False):
        report["skipped"] = "evolution_disabled_for_user"
        return report

    views = load_personal_episodes(user_id, limit=limit)
    report["episodes"] = len(views)
    if not views:
        return report

    patterns = _discover(views)
    report["patterns"] = len(patterns)
    constraints = P.extract_ordering_constraints(
        views, min_support=PERSONAL_MIN_SUPPORT, min_contexts=1
    )
    report["ordering_constraints"] = [c.to_dict() for c in constraints]

    known_concepts = _known_concepts(prefs)

    for pattern in patterns:
        if pattern.kind != P.PATTERN_SUCCESS_SUBSEQUENCE or not pattern.tool_sequence:
            continue
        # 这是"多次成功里观察到的共性"，不是某次失败的归因，所以走聚合决策。
        # 旧实现在这里伪造 outcome_verdict="failed" 逼归因器选中技能引擎——
        # 正是 credit.aggregate_finding 的 docstring 点名批判的做法：评审台
        # 因此展示了一句没人测量过的失败解释。
        decision = aggregate_finding(
            target=T_SKILL,
            explanation=(
                f"{pattern.support} 个成功 Episode 复现同一 "
                f"{len(pattern.tool_sequence)} 步工具序列"
                f"（成功率 {pattern.success_rate:.0%}），观察自本人历史会话"
            ),
            confidence=confidence_from_evidence(pattern.support, saturates_at=10),
            evidence_count=pattern.support,
        )
        if not decision.may_produce_candidate:
            report["no_update"] += 1
            continue

        # min_support 必须显式传入：发现阶段用的个人门槛（3）在 _discover 返回
        # 后已恢复为全局值（5），不传的话支持度 3–4 的个人模式会在这里被静默拒绝。
        proposal = P.promote_tool_sequence_to_skill(
            pattern,
            episodes=views,
            min_support=PERSONAL_MIN_SUPPORT,
            skill_credit=decision.scores.get("skill", 0.0),
            workflow_credit=decision.scores.get("workflow", 0.0),
        )
        if proposal is None:
            continue

        # 规则门之后的 LLM 语义裁决：意图簇是否巧合归并、序列是否只是对任何
        # 任务都成立的通用组合。拒绝即不产候选；模型不可用同样拒绝，但在
        # 报告里可区分。
        from core.evolution.loop import _run_async
        from core.evolution.sop_judge import adjudicate_sequence_sop

        covered = {str(e) for e in (proposal.payload.get("episode_ids") or [])}
        verdict = _run_async(
            adjudicate_sequence_sop(
                representative=str(proposal.payload.get("intent") or ""),
                objectives=[
                    str(v.get("objective") or "")
                    for v in views
                    if str(v.get("episode_id") or "") in covered
                ],
                tool_sequence=[str(t) for t in proposal.payload.get("tool_sequence", [])],
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

        asset_id = "u" + hashlib.sha256(
            f"{user_id}|{proposal.signature}".encode("utf-8")
        ).hexdigest()[:12]

        ir = EvolutionIR(
            target_kind="skill",
            target_asset_id=asset_id,
            base_version="v0",
            operation="new",
            hypothesis=proposal.rationale,
            changes=[
                {
                    "tool_sequence": proposal.payload.get("tool_sequence", []),
                    "ordering_constraints": [
                        c.to_dict()
                        for c in constraints
                        if c.before in proposal.payload.get("tool_sequence", [])
                        and c.after in proposal.payload.get("tool_sequence", [])
                    ],
                }
            ],
            evidence_refs=proposal.payload.get("episode_ids", []),
            # A capability that reaches one person carries less risk than one
            # that reaches everyone, and the tier drives how much verification
            # is demanded before it may activate.
            risk_tier=RISK_LOW,
            scope={"level": "user", "user_id": user_id},
            rollback=Rollback(version_id="v0", triggers=["user_disable"]),
            evaluation_plan={"replay_set": "personal", "primary_metric": "task_success"},
        )

        stored_credit = {**decision.to_dict(), "sop_judge": verdict.to_dict()}
        try:
            validate_ir(
                ir,
                credit_decision=stored_credit,
                allowed_scopes=["user", "workspace"],
                known_concepts=known_concepts,
            )
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
            credit=stored_credit,
            proposer=f"personal:{user_id}",
            evidence_refs=proposal.payload.get("episode_ids", []),
        )
        if candidate_id:
            report["candidates"].append(
                {"candidate_id": candidate_id, "signature": proposal.signature}
            )

    # The other engines, scoped to this user. Running them only in the
    # fleet-wide cycle meant the community edition — where the fleet cycle does
    # not run at all — had memory reorganisation and capability retirement
    # switched off entirely, which is not an edition boundary anyone chose.
    report.update(_personal_other_engines(user_id, views, dry_run=dry_run))

    logger.info(
        "[personal-evo] user=%s episodes=%d patterns=%d candidates=%d",
        user_id,
        report["episodes"],
        report["patterns"],
        len(report["candidates"]),
    )
    return report


def _personal_other_engines(
    user_id: str, views: List[Dict[str, Any]], *, dry_run: bool
) -> Dict[str, Any]:
    """Memory, intent and decremental, for one person's own evidence.

    Orchestration and prompt engines are deliberately absent. Both change what
    *every* request in scope is assembled from, so a single user's evidence is
    not a basis for them — this is the scope boundary, not a missing feature.
    """
    from core.evolution.cycle_extras import run_extras
    from core.evolution.loop import (
        _decremental_candidates,
        _intent_skill_candidates,
        _memory_candidates,
        _memory_scan_findings,
    )

    scan_findings = _memory_scan_findings(views)
    extras = run_extras(views, scan_findings=scan_findings)

    produced: List[Dict[str, Any]] = []
    for name, fn in (
        ("memory", lambda: _memory_candidates(extras, views, dry_run=dry_run)),
        ("intent", lambda: _intent_skill_candidates(extras, views, dry_run=dry_run)),
        ("decremental", lambda: _decremental_candidates(extras, dry_run=dry_run)),
    ):
        try:
            produced.extend(fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[personal-evo] %s engine failed for %s: %s", name, user_id, exc)
            produced.append({"kind": name, "error": str(exc)})

    memory_report = dict(extras.get("memory") or {})
    memory_report.pop("ops", None)
    extras["memory"] = memory_report
    return {"extras": extras, "engine_candidates": produced}


def _known_concepts(prefs: Dict[str, Any]) -> Optional[List[str]]:
    """Concepts to validate candidates against, per the user's ontology choice.

    Returns ``None`` when no pack applies, which tells the validator to skip the
    concept check entirely rather than fail every candidate. Plenty of
    deployments have no domain pack at all, and evolution must work there.
    """
    mode = prefs.get("ontology_mode") or "none"
    if mode == "none":
        return None

    try:
        from core.db.models import OntologyPack, OntologyPackVersion

        with SessionLocal() as db:
            query = db.query(OntologyPack).filter(OntologyPack.active_version_id.isnot(None))
            if mode == "specific":
                pack_id = prefs.get("ontology_pack_id") or ""
                if not pack_id:
                    return None
                query = query.filter(OntologyPack.pack_id == pack_id)

            version_ids = [p.active_version_id for p in query.all()]
            if not version_ids:
                return None

            concepts: List[str] = []
            for version in (
                db.query(OntologyPackVersion)
                .filter(OntologyPackVersion.version_id.in_(version_ids))
                .all()
            ):
                content = getattr(version, "content", None) or {}
                if isinstance(content, dict):
                    for item in content.get("concepts") or []:
                        name = item.get("name") if isinstance(item, dict) else item
                        if name:
                            concepts.append(str(name))
            return concepts or None
    except Exception as exc:
        logger.debug("[personal-evo] concept lookup failed: %s", exc)
        return None
