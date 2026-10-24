"""Skill usage, canary uplift and exposure measured from episode evidence."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging
from core.evolution.cycle import cycle_extras_constants

logger = logging.getLogger(__name__)

def is_after(created_at: Any, activated_at: Any) -> bool:
    from datetime import timezone

    if created_at is None or activated_at is None:
        return False
    left = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    right = activated_at if activated_at.tzinfo else activated_at.replace(tzinfo=timezone.utc)
    return left >= right


def recent_activations() -> List[Any]:
    """``(asset_id, activated_at, related_task_types)`` for live releases.

    ``related_task_types`` comes from the release's own scope: a dip on the task
    the change targeted is an ordinary regression its guardrail already watches,
    and treating it as negative transfer would bury the collateral damage this
    is looking for in noise from the intended effect.
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionRelease
        from core.evolution.cycle.release import ACTIVE, CANARY

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


def declared_tools_for(skill_ids: Sequence[str]) -> Dict[str, List[str]]:
    """Each skill's own declared tools, read from the skill itself."""
    tools: Dict[str, List[str]] = {}
    try:
        from core.capabilities.skills.loader_factory import get_skill_loader

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

    declared_by_skill = declared_tools_for(
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

    uplift_by_skill = uplift_from_canary(views)
    for skill_id, entry in stats.items():
        opens = entry["opens"]
        measurable = entry["opens_measurable"]
        entry["success_rate"] = entry["opens_succeeded"] / opens if opens else 0.0
        entry["follow_through"] = (
            entry["opens_followed_through"] / measurable if measurable else None
        )
        entry["exposure_days"] = exposure_days(skill_id)
        entry["uplift"] = uplift_by_skill.get(skill_id)
    return stats


def canary_releases() -> List[Tuple[str, str, int, Any]]:
    """``(asset_id, release_id, traffic_percent, started_at)`` for live canaries."""
    from core.evolution.cycle.release import CANARY

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


def uplift_from_canary(
    views: Sequence[Dict[str, Any]],
    *,
    ramping: Optional[Sequence[Tuple[str, str, int, Any]]] = None,
) -> Dict[str, Optional[float]]:
    """"Is the deployment better off with this skill?", answered by comparison.

    A canary release already splits users deterministically into "sees it" and
    "does not" (:func:`core.evolution.cycle.release.in_canary_bucket`). That split is
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
    from core.evolution.cycle.release import in_canary_bucket

    if ramping is None:
        ramping = canary_releases()

    uplift: Dict[str, Optional[float]] = {}
    for skill_id, release_id, traffic, started_at in ramping:
        exposed: List[bool] = []
        withheld: List[bool] = []
        for view in views:
            created_at = view.get("created_at")
            if created_at is None or started_at is None:
                continue
            if not is_after(created_at, started_at):
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
        if len(exposed) < cycle_extras_constants.MIN_UPLIFT_SAMPLES or len(withheld) < cycle_extras_constants.MIN_UPLIFT_SAMPLES:
            uplift[skill_id] = None
            continue
        uplift[skill_id] = (sum(exposed) / len(exposed)) - (
            sum(withheld) / len(withheld)
        )
    return uplift


def exposure_days(skill_id: str) -> float:
    """How long this skill has been exposed to anyone.

    Read from the release ledger. Without it, "never opened" cannot be told
    apart from "shipped yesterday", and a retirement rule that cannot tell them
    apart retires whatever is newest.
    """
    from datetime import datetime, timezone

    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionRelease
        from core.evolution.cycle.release import ACTIVE, CANARY

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
