"""The two engines the cycle was missing, and the direction the third lacked.

A cycle used to do exactly one thing: grow the skill library from identical tool
sequences. Memory reorganisation and capability retirement were implemented and
unreachable, and recurrence detected from *what the user asked* did not exist at
all.

Wiring them up is not enough on its own, and the first attempt at it showed why.
Three failures worth naming, because they are the ones this file now exists to
not repeat:

* **A finding with no exit changes nothing.** The first version returned a
  report that no code read. Everything here now produces
  :class:`~core.evolution.ir.EvolutionIR` candidates that go through the same
  security gate, review queue and release ladder as every other change. A
  proposal that cannot reach the queue is a log line.
* **Retrieval frequency is not duplication.** Counting how often one content
  hash was retrieved and calling four hits a duplicate inverts the meaning: a
  memory retrieved in four separate conversations is the store's most useful
  entry, and merging it away is a proposal to delete what works. Duplication is
  a property of *inventory*, so it is found by scanning the store
  (:mod:`core.evolution.memory_scan`), which the event stream cannot do.
* **Being loaded is not being used.** Retirement read a "selections" count that
  every enabled skill shared, so it ranked skills by how recently they shipped.
  Usage is ``skill.opened`` — the model deciding for itself to read the
  document — and follow-through is whether the plan afterwards matched what the
  document said.

Everything here emits **candidates**. Nothing activates and nothing deletes: the
reverse direction is where over-eager automation does damage that is hard to
notice, because removing a capability produces no error, only a slow decline.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# An intent must recur on this many separate occasions before "we keep
# re-planning this" is a claim about behaviour rather than about one bad
# afternoon of rephrasing.
MIN_INTENT_OCCASIONS = 3
# Below this success rate a recurring intent is a *failure* pattern: the right
# response is a fix proposal, not a distilled skill blessing the current plan.
INTENT_MIN_SUCCESS_RATE = 0.6
# A recurring intent handled the same way every time needs no skill — the plan
# is already stable. The signal is variety.
MIN_PLAN_VARIETY = 2

# Injected this often that the association is not two unlucky turns.
DEMOTE_MIN_HITS = 4
# At or below this success rate, retrieving it is not helping.
DEMOTE_SUCCESS_CEILING = 0.34

# Turns required in *each* arm before a canary comparison is reported. Below
# this the difference between two rates is noise, and this particular number can
# trigger an immediate rollback — so it has the strictest sample requirement of
# anything the decremental engine computes.
MIN_UPLIFT_SAMPLES = 10


def _asset_id(prefix: str, seed: str) -> str:
    return f"{prefix}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


# ── Skills: upward, from recurring intent ────────────────────────────────────


def intent_findings(views: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Recurring requests whose handling never settled.

    The pairing matters. Recurrence alone says the question is common;
    recurrence **with plan variety** says the system has not found a stable way
    to answer it and is paying the planning cost fresh every time. That second
    condition is what distinguishes a capability gap from a popular question.
    """
    from core.evolution.similarity import cluster_intents

    clusters, method = cluster_intents(views, min_support=MIN_INTENT_OCCASIONS)
    findings: List[Dict[str, Any]] = []
    for cluster in clusters:
        if cluster.occasions < MIN_INTENT_OCCASIONS:
            # Recurrence inside one conversation is a user rephrasing after a
            # bad answer, which is evidence about that answer, not about demand.
            continue
        if cluster.plan_variety < MIN_PLAN_VARIETY:
            # Same question, same plan, every time. Already effectively a skill;
            # writing one down adds an asset to maintain and changes nothing.
            continue
        if cluster.success_rate < INTENT_MIN_SUCCESS_RATE:
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
                "asset_id": _asset_id("auto-intent", cluster.representative),
                "reason": (
                    f"同一意图在 {cluster.occasions} 次独立会话中出现，却用了 "
                    f"{cluster.plan_variety} 种不同的工具序列——每次都在重新规划"
                ),
            }
        )
    return {"method": method, "clusters": len(clusters), "findings": findings}


# ── Skills: downward ─────────────────────────────────────────────────────────


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
    from core.evolution import promotion as P

    stats = skill_stats if skill_stats is not None else skill_usage_from_views(views)

    overlaps: List[Tuple[str, str, int]] = []
    if declared_tools is None:
        declared_tools = _declared_tools_for(sorted(stats))
    if declared_tools:
        overlaps = P.find_overlaps(declared_tools)

    retirements = P.find_retirement_candidates(stats)
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
    activations = _recent_activations()
    if not activations:
        return []

    findings: List[Dict[str, Any]] = []
    for asset_id, activated_at, related in activations:
        split = [
            {**view, "after": _is_after(view.get("created_at"), activated_at)}
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


def _is_after(created_at: Any, activated_at: Any) -> bool:
    from datetime import timezone

    if created_at is None or activated_at is None:
        return False
    left = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    right = activated_at if activated_at.tzinfo else activated_at.replace(tzinfo=timezone.utc)
    return left >= right


def _recent_activations() -> List[Any]:
    """``(asset_id, activated_at, related_task_types)`` for live releases.

    ``related_task_types`` comes from the release's own scope: a dip on the task
    the change targeted is an ordinary regression its guardrail already watches,
    and treating it as negative transfer would bury the collateral damage this
    is looking for in noise from the intended effect.
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionRelease
        from core.evolution.release import ACTIVE, CANARY

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionRelease)
                .filter(EvolutionRelease.stage.in_([ACTIVE, CANARY]))
                .order_by(EvolutionRelease.created_at.desc())
                .limit(20)
                .all()
            )
        return [
            (
                str(row.target_asset_id),
                row.created_at,
                [str(t) for t in ((row.scope_filter or {}).get("task_types") or [])],
            )
            for row in rows
            if row.created_at is not None
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cycle-extras] activation ledger unavailable: %s", exc)
        return []


def _declared_tools_for(skill_ids: Sequence[str]) -> Dict[str, List[str]]:
    """Each skill's own declared tools, read from the skill itself."""
    tools: Dict[str, List[str]] = {}
    try:
        from core.agent_skills.loader import get_skill_loader

        metadata = get_skill_loader().load_all_metadata()
    except Exception as exc:  # noqa: BLE001
        logger.info("[cycle-extras] skill metadata unavailable, overlap skipped: %s", exc)
        return {}
    for skill_id in skill_ids:
        meta = metadata.get(skill_id)
        declared = list(getattr(meta, "allowed_tools", None) or []) if meta else []
        if declared:
            tools[skill_id] = [str(t) for t in declared]
    return tools


def skill_usage_from_views(views: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """How each skill actually fared, from evidence that distinguishes use from exposure.

    ``offers`` counts turns where the skill was **available** to the model — its
    name and description were in the prompt. Skill loading offers every enabled
    skill, so this is a denominator, not a choice: it answers "how many chances
    did this skill have to be picked", which is exactly what makes "never
    opened" mean something.

    ``opens`` is the choice. The gap between the two is the whole signal.
    """
    stats: Dict[str, Dict[str, Any]] = {}

    def bucket(skill_id: str) -> Dict[str, Any]:
        return stats.setdefault(
            str(skill_id),
            {
                "offers": 0,
                "opens": 0,
                "opens_succeeded": 0,
                "opens_followed_through": 0,
                "opens_measurable": 0,
                "exposure_days": 0.0,
            },
        )

    declared_by_skill = _declared_tools_for(
        sorted(
            {
                str(opened.get("skill_id") or "")
                for view in views
                for opened in (view.get("skills_opened") or [])
                if opened.get("skill_id")
            }
        )
    )

    for view in views:
        for skill_id in view.get("skill_sequence") or []:
            bucket(skill_id)["offers"] += 1

        succeeded = view.get("verdict") == "success"
        for opened in view.get("skills_opened") or []:
            skill_id = str(opened.get("skill_id") or "")
            if not skill_id:
                continue
            entry = bucket(skill_id)
            entry["opens"] += 1
            if succeeded:
                entry["opens_succeeded"] += 1

            # Follow-through: after reading the document, did the model do what
            # the document is about? Approximated by whether the tools it went
            # on to call are among the ones the skill declares.
            #
            # Read from the skill itself, not from the event — an event cannot
            # carry what the skill declared *now*, and a stale copy would score
            # a rewritten skill against its old tool list.
            declared = {str(t) for t in (declared_by_skill.get(skill_id) or [])}
            after = [str(t) for t in (opened.get("tools_after_open") or [])]
            if not declared or not after:
                # A skill that declares no tools (a rules-only skill shapes
                # ordering, not tool choice) or a turn that called nothing after
                # the open cannot be measured this way. Excluded from the
                # denominator rather than counted as a pass — counting them as
                # passes is what made follow-through 100% for everything and the
                # "opened but ignored" verdict unreachable.
                continue
            entry["opens_measurable"] += 1
            if any(tool in declared for tool in after):
                entry["opens_followed_through"] += 1

    uplift_by_skill = _uplift_from_canary(views)
    for skill_id, entry in stats.items():
        opens = entry["opens"]
        measurable = entry["opens_measurable"]
        entry["success_rate"] = entry["opens_succeeded"] / opens if opens else 0.0
        entry["follow_through"] = (
            entry["opens_followed_through"] / measurable if measurable else None
        )
        entry["exposure_days"] = _exposure_days(skill_id)
        entry["uplift"] = uplift_by_skill.get(skill_id)
    return stats


def _canary_releases() -> List[Tuple[str, str, int, Any]]:
    """``(asset_id, release_id, traffic_percent, started_at)`` for live canaries."""
    from core.evolution.release import CANARY

    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionRelease

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionRelease)
                .filter(
                    EvolutionRelease.target_kind == "skill",
                    EvolutionRelease.stage == CANARY,
                )
                .all()
            )
        return [
            (
                str(row.target_asset_id),
                str(row.release_id),
                int(row.traffic_percent or 0),
                row.created_at,
            )
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cycle-extras] canary ledger unavailable: %s", exc)
        return []


def _uplift_from_canary(
    views: Sequence[Dict[str, Any]],
    *,
    ramping: Optional[Sequence[Tuple[str, str, int, Any]]] = None,
) -> Dict[str, Optional[float]]:
    """"Is the deployment better off with this skill?", answered by comparison.

    A canary release already splits users deterministically into "sees it" and
    "does not" (:func:`core.evolution.release.in_canary_bucket`). That split is
    the comparison: same period, same task mix, same everything except exposure
    to this one asset. Recomputing bucket membership here rather than storing it
    per request means the analysis works on history too.

    Returns ``None`` for a skill with no usable comparison. ``None`` and ``0.0``
    are different answers — "we have not measured" must never read as "measured,
    no effect", because the second one licenses a rollback decision and the
    first one does not.

    ``ramping`` is injectable so this is testable against the real bucketing
    rather than against a re-implementation of it in a test helper.
    """
    from core.evolution.release import in_canary_bucket

    if ramping is None:
        ramping = _canary_releases()

    uplift: Dict[str, Optional[float]] = {}
    for skill_id, release_id, traffic, started_at in ramping:
        exposed: List[bool] = []
        withheld: List[bool] = []
        for view in views:
            created_at = view.get("created_at")
            if created_at is None or started_at is None:
                continue
            if not _is_after(created_at, started_at):
                continue
            user_id = str(view.get("user_id") or "")
            if not user_id:
                continue
            succeeded = view.get("verdict") == "success"
            if in_canary_bucket(
                release_id=release_id, subject_id=user_id, traffic_percent=traffic
            ):
                exposed.append(succeeded)
            else:
                withheld.append(succeeded)

        # Both arms need enough turns for a rate to mean anything. A one-sided
        # or tiny comparison produces a number that looks like evidence and is
        # not, and this number can trigger an immediate rollback.
        if len(exposed) < MIN_UPLIFT_SAMPLES or len(withheld) < MIN_UPLIFT_SAMPLES:
            uplift[skill_id] = None
            continue
        uplift[skill_id] = (sum(exposed) / len(exposed)) - (
            sum(withheld) / len(withheld)
        )
    return uplift


def _exposure_days(skill_id: str) -> float:
    """How long this skill has been exposed to anyone.

    Read from the release ledger. Without it, "never opened" cannot be told
    apart from "shipped yesterday", and a retirement rule that cannot tell them
    apart retires whatever is newest.
    """
    from datetime import datetime, timezone

    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionRelease
        from core.evolution.release import ACTIVE, CANARY

        with SessionLocal() as db:
            row = (
                db.query(EvolutionRelease)
                .filter(
                    EvolutionRelease.target_kind == "skill",
                    EvolutionRelease.target_asset_id == skill_id,
                    EvolutionRelease.stage.in_([ACTIVE, CANARY]),
                )
                .order_by(EvolutionRelease.created_at.asc())
                .first()
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cycle-extras] exposure lookup failed for %s: %s", skill_id, exc)
        return 0.0
    if row is None or row.created_at is None:
        # Not an evolved skill, or never released through the ladder. A
        # hand-authored skill has always been exposed, so judging it on its
        # observed usage is exactly right.
        return float("inf")
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400.0


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
    from core.evolution import promotion as P

    by_type_before: Dict[str, List[bool]] = {}
    by_type_after: Dict[str, List[bool]] = {}
    for view in views:
        target = by_type_after if view.get(after_key) else by_type_before
        target.setdefault(str(view.get("task_type") or "chat"), []).append(
            view.get("verdict") == "success"
        )

    def rates(source: Dict[str, List[bool]]) -> Dict[str, float]:
        return {k: (sum(v) / len(v) if v else 0.0) for k, v in source.items()}

    findings = P.detect_negative_transfer(
        asset_id=asset_id or "unspecified",
        # Only *unrelated* task types are examined. A dip on the task the change
        # targeted is an ordinary regression its own guardrail catches; the
        # dangerous case is collateral damage where nobody is looking.
        related_task_types=list(related_task_types),
        before=rates(by_type_before),
        after=rates(by_type_after),
    )
    return [f.to_dict() for f in findings]


# ── Memory: the half that makes it evolution rather than accumulation ────────


def memory_findings(
    views: Sequence[Dict[str, Any]],
    *,
    scan_findings: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Reorganisation proposals for the memory layer.

    Two independent sources, because they answer different questions and only
    one of them is answerable from a run's evidence:

    * **inventory** (``scan_findings``) — duplicates, contradictions and stale
      entries, which require reading the store;
    * **behaviour** (``views``) — memories that keep being injected into turns
      that fail, which requires reading the runs.

    The behavioural signal is deliberately *not* framed as duplication. A
    memory injected repeatedly is either helping or not, and the difference is
    the outcome of the turns it landed in — never the count on its own.
    """
    from core.evolution.memory_ops import (
        OP_MERGE,
        OP_REWEIGHT,
        SCOPE_USER,
        MemoryOp,
        partition_by_approval,
    )
    from core.evolution.memory_scan import (
        FINDING_CONTRADICTION,
        FINDING_DUPLICATE,
        FINDING_STALE,
    )

    retrieved: Dict[str, int] = {}
    helped: Dict[str, int] = {}
    for view in views:
        for ref in view.get("memory_refs") or []:
            key = str(ref)
            retrieved[key] = retrieved.get(key, 0) + 1
            if view.get("verdict") == "success":
                helped[key] = helped.get(key, 0) + 1

    ops: List[MemoryOp] = []
    contradictions: List[Dict[str, Any]] = []

    # 1. Inventory findings, from an actual scan of the store.
    for finding in scan_findings or []:
        payload = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
        kind = payload.get("kind")
        refs = [str(r) for r in (payload.get("refs") or [])]
        if kind == FINDING_DUPLICATE and len(refs) >= 2:
            # The second entry loses. Content is not rewritten and nothing is
            # removed — the survivor already says the same thing.
            ops.append(
                MemoryOp(
                    operation=OP_MERGE,
                    memory_ref=refs[1],
                    scope=SCOPE_USER,
                    reason=payload.get("reason", ""),
                    before={"injected": True, "duplicate_of": refs[0]},
                    after={"injected": False, "superseded_by": refs[0]},
                )
            )
        elif kind == FINDING_STALE and refs:
            ops.append(
                MemoryOp(
                    operation=OP_REWEIGHT,
                    memory_ref=refs[0],
                    scope=SCOPE_USER,
                    reason=payload.get("reason", ""),
                    before={"weight": 1.0},
                    after={"weight": 0.7},
                )
            )
        elif kind == FINDING_CONTRADICTION:
            # Neither side is automatically right, and picking by recency is
            # how a corrected mistake becomes the new truth. This is reported
            # for a human, with both sides and their dates.
            contradictions.append(payload)

    # 2. Behavioural: injected often, associated with failure. Down-weighted,
    #    never deleted — a wrong reweight is recoverable and a wrong deletion is
    #    not.
    for ref, count in retrieved.items():
        if count < DEMOTE_MIN_HITS:
            continue
        rate = helped.get(ref, 0) / count
        if rate <= DEMOTE_SUCCESS_CEILING:
            ops.append(
                MemoryOp(
                    operation=OP_REWEIGHT,
                    memory_ref=ref,
                    scope=SCOPE_USER,
                    reason=(
                        f"被注入 {count} 次，其中仅 {helped.get(ref, 0)} 次所在回答成功"
                        "——检索到它并没有帮上忙"
                    ),
                    before={"weight": 1.0},
                    after={"weight": 0.5},
                )
            )

    auto, needs_review = partition_by_approval(ops)
    return {
        "distinct_memories_retrieved": len(retrieved),
        "merge_proposals": sum(1 for op in ops if op.operation == OP_MERGE),
        "reweight_proposals": sum(1 for op in ops if op.operation == OP_REWEIGHT),
        "contradictions": contradictions,
        # Reported separately because the split *is* the governance boundary:
        # reweighting is reversible and may proceed, merging changes what gets
        # injected and may not.
        "auto_applicable": [op.to_dict() for op in auto],
        "needs_review": [op.to_dict() for op in needs_review],
        "ops": ops,
    }


# ── One entry point for the cycle ────────────────────────────────────────────


def run_extras(
    views: Sequence[Dict[str, Any]],
    *,
    scan_findings: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Everything the cycle was missing, in one call.

    Each section is independently guarded: a failure in the decremental pass
    must not cost the intent pass its findings, because they answer different
    questions and an operator needs whichever ones succeeded.
    """
    report: Dict[str, Any] = {}
    for name, fn in (
        ("intent", lambda: intent_findings(views)),
        ("decremental", lambda: decremental_findings(views)),
        ("memory", lambda: memory_findings(views, scan_findings=scan_findings)),
    ):
        try:
            report[name] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cycle-extras] %s pass failed: %s", name, exc)
            report[name] = {"error": str(exc)}
    return report
