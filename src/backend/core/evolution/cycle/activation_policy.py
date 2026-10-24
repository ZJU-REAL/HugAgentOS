"""Gates on activation: freeze switch, pacing, entry stage and rollback snapshots."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from core.evolution.cycle import activation_constants
from core.evolution.cycle import activation_types

def capture_previous(db, skill_id: str) -> Optional[Dict[str, Any]]:
    """Snapshot the current state so rollback has a concrete target."""
    from core.db.models import AdminSkill

    row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    if row is None:
        return None
    return {
        "skill_id": row.skill_id,
        "skill_content": row.skill_content,
        "display_name": row.display_name,
        "description": row.description,
        "version": row.version,
        "tags": list(row.tags or []),
        "allowed_tools": list(row.allowed_tools or []),
        "extra_files": dict(row.extra_files or {}),
        "is_enabled": bool(row.is_enabled),
    }


def is_frozen(db) -> bool:
    """Global stop switch, readable without a redeploy.

    An incident needs a way to stop every layer activating *now*, so it lives in
    ``system_configs`` rather than in an environment variable.
    """
    try:
        from core.db.models import SystemConfig

        row = (
            db.query(SystemConfig.config_value)
            .filter(SystemConfig.config_key == activation_constants.FREEZE_CONFIG_KEY)
            .first()
        )
        return bool(row) and str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 - absence of the row means "not frozen"
        return False


def skill_layer_state(db) -> Tuple[Optional[datetime], int]:
    """When a skill last went live, and how many releases are mid-flight.

    Read from the release ledger rather than kept in memory: a per-process
    limiter resets on every restart and multiplies by the number of replicas,
    which is exactly how a cooldown quietly stops existing.
    """
    from core.db.models.evolution import EvolutionRelease
    from core.evolution.cycle.release import ACTIVE, IN_FLIGHT_STAGES

    last_row = (
        db.query(EvolutionRelease.created_at)
        .filter(
            EvolutionRelease.target_kind == "skill",
            EvolutionRelease.stage == ACTIVE,
        )
        .order_by(EvolutionRelease.created_at.desc())
        .first()
    )
    in_flight = (
        db.query(EvolutionRelease)
        .filter(EvolutionRelease.stage.in_(list(IN_FLIGHT_STAGES)))
        .count()
    )
    last = last_row[0] if last_row else None
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last, int(in_flight or 0)


def check_pace(db, *, tenant_id: str, layer: str = "skill") -> None:
    """Enforce the multi-timescale gate the design declares.

    Skills sit on an hours-to-days cadence so the system does not become
    non-stationary. That was a documented number with no enforcement: nothing
    consulted ``timescales`` before writing a live asset, so ten activations
    could land in one minute and no metric movement afterwards would be
    attributable to any of them.
    """
    from core.evolution.cycle.timescales import RateLimiter

    last, in_flight = skill_layer_state(db)
    limiter = RateLimiter(frozen=is_frozen(db))
    if last is not None:
        limiter.last_activation[layer] = last
    allowed, reason = limiter.may_activate(
        layer, tenant=tenant_id or "default", concurrent_activations=in_flight
    )
    if not allowed:
        raise activation_types.ActivationError("pace_limited", f"节奏闸门拒绝本次激活：{reason}")


def entry_stage(
    candidate,
    *,
    approver: str,
    force: bool,
    owner_user_id: Optional[str],
) -> Tuple[str, int]:
    """The stage this candidate has actually earned, and its traffic share.

    Previously every activation wrote ``active`` at 100%, whatever the risk tier
    said. ``ir.MIN_VERIFICATION`` declares that a high-risk change owes replay,
    shadow, canary *and* human approval before going live, and a brand-new
    skill is exactly that tier — so the old behaviour skipped two of the four
    stages its own IR demanded.

    A personal activation is the one case that legitimately goes straight live:
    its blast radius is one consenting user, and the ramp exists to bound blast
    radius. It is recorded as scoped rather than as full traffic.
    """
    from core.evolution.ir import RISK_LOW, required_verification
    from core.evolution.cycle.release import ACTIVE, REPLAY_PASSED, SHADOW

    risk = str(candidate.risk_tier or "").strip() or RISK_LOW
    replayed = candidate.status in (REPLAY_PASSED, ACTIVE)

    if not replayed:
        # ``force`` waives the replay *stage* — and only for the tier whose
        # verification requirement is replay alone. Waiving it for a tier that
        # also owes shadow and canary is not a shortcut, it is the whole gate.
        if not force:
            raise activation_types.ActivationError(
                "verification_incomplete",
                f"候选尚未通过回放（当前 {candidate.status}）",
            )
        if risk != RISK_LOW:
            raise activation_types.ActivationError(
                "force_forbidden_for_risk",
                f"force 只允许用于 low 风险候选，本候选为 {risk}；"
                f"需完成 {list(required_verification(risk))}",
            )

    if owner_user_id:
        return ACTIVE, 0

    if set(required_verification(risk)) <= {"replay"}:
        return ACTIVE, 100

    # Higher tiers enter at shadow: the asset exists and is measurable, and
    # reaches nobody until an operator ramps it.
    return SHADOW, 0


def has_graph_conflict(candidate) -> bool:
    """Whether the candidate's manifest flags contradictory L3 evidence."""
    if candidate is None:
        return False
    for change in (getattr(candidate, "ir", None) or {}).get("changes") or []:
        manifest = change.get("manifest") if isinstance(change, dict) else None
        if isinstance(manifest, dict) and manifest.get("graph_conflict"):
            return True
    return False


def next_version(previous: Optional[Dict[str, Any]]) -> str:
    if not previous:
        return "1.0.0"
    parts = str(previous.get("version") or "1.0.0").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        return "1.0.1"


def json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def source_candidate_for(db, skill_id: str) -> str:
    """The candidate that first materialised this skill.

    Memory demotions are recorded against that candidate, so it is the key that
    finds them again when the skill is retired.
    """
    from core.db.models.evolution import EvolutionRelease

    row = (
        db.query(EvolutionRelease)
        .filter(
            EvolutionRelease.target_kind == "skill",
            EvolutionRelease.target_asset_id == skill_id,
        )
        .order_by(EvolutionRelease.created_at.asc())
        .first()
    )
    return str(row.candidate_id) if row is not None else ""
