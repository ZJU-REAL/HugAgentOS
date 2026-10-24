"""Pick the agent profile that governs a run."""
from __future__ import annotations
from core.evolution.profiles import agent_profile
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging
logger = logging.getLogger(__name__)

def scope_rank(scope: Dict[str, str], *, user_id: str, tenant_id: str) -> Optional[int]:
    """How specific this profile is to the subject, or ``None`` if it is not.

    Lower is more specific. Returning ``None`` for a profile owned by another
    subject is the whole point: it must not act as a fallback.
    """
    owner = str((scope or {}).get("user_id") or "")
    tenant = str((scope or {}).get("tenant_id") or "")
    if owner:
        return 0 if owner == user_id else None
    if tenant:
        return 1 if tenant == (tenant_id or "default") else None
    return 2


def pick_profile(
    profiles: Sequence[agent_profile.AgentProfile],
    *,
    task_type: str,
    user_id: str = "",
    tenant_id: str = "default",
) -> Optional[agent_profile.AgentProfile]:
    """The one profile that governs this run, or ``None``.

    A task-type-specific profile beats a general one at the same scope level:
    the narrower statement is the more deliberate one.
    """
    ranked: List[Tuple[int, int, agent_profile.AgentProfile]] = []
    for profile in profiles:
        if not profile.applies_to(task_type):
            continue
        rank = scope_rank(profile.scope, user_id=user_id, tenant_id=tenant_id)
        if rank is None:
            continue
        ranked.append((rank, 0 if profile.task_types else 1, profile))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def profile_from_row(row: Any) -> agent_profile.AgentProfile:
    """The profile a stored row describes, with the columns believed over payload.

    ``profile_id`` / ``version`` / ``task_types`` / ``scope`` exist as real
    columns *and* inside ``payload`` (:meth:`AgentProfile.to_dict` writes them),
    so for anything published through :mod:`core.evolution.cycle.activation` the two
    agree and this is a no-op. They stop agreeing for a row written by hand or
    by a seeding script, and reading only ``payload`` then fails open in the
    worst possible direction: a missing ``task_types`` parses as ``[]``, which
    :meth:`AgentProfile.applies_to` treats as *every* task type, and a missing
    ``scope`` parses as ``{}``, which :func:`_scope_rank` treats as *every*
    subject. A profile published for one task type silently governs every run —
    reported under the id ``builtin``, because that is what
    :meth:`AgentProfile.from_dict` falls back to, so the log line naming the
    culprit names the deployment default instead.

    The two narrowing fields resolve to whichever side is non-empty rather than
    to the column unconditionally: an empty column against a populated payload
    is the same widening failure with the sides swapped. When both are populated
    and disagree the column wins — it is what the activation uniqueness check and
    the console read — and the disagreement is logged, because it means something
    wrote this row outside the activation path.
    """
    raw = dict(row.payload or {})

    # Identity: the column is the primary key and NOT NULL, so it is always the
    # more trustworthy of the two, and a wrong id here is what makes a bad
    # profile untraceable in the logs.
    raw["profile_id"] = row.profile_id
    raw["version"] = row.version

    col_types = [str(t) for t in (row.task_types or [])]
    payload_types = [str(t) for t in (raw.get("task_types") or [])]
    if col_types and payload_types and set(col_types) != set(payload_types):
        logger.warning(
            "[agent-profile] %s: task_types column %s != payload %s, using column",
            row.profile_id, col_types, payload_types,
        )
    raw["task_types"] = col_types or payload_types

    col_scope = dict(row.scope or {})
    payload_scope = dict(raw.get("scope") or {})
    if col_scope and payload_scope and col_scope != payload_scope:
        logger.warning(
            "[agent-profile] %s: scope column %s != payload %s, using column",
            row.profile_id, col_scope, payload_scope,
        )
    raw["scope"] = col_scope or payload_scope

    return agent_profile.AgentProfile.from_dict(raw)


def load_active_profile(
    *,
    task_type: str = "chat",
    user_id: str = "",
    tenant_id: str = "default",
) -> agent_profile.AgentProfile:
    """The profile in force for this run.

    Read fresh on every call. Resolving once at import — which is what the old
    orchestration policy did — means a published change needs a process restart
    to take effect and different replicas disagree in the meantime, neither of
    which is acceptable for an asset whose entire purpose is to change behaviour
    between runs.
    """
    from core.db.engine import SessionLocal
    from core.db.models.evolution import EvolutionAgentProfile

    try:
        with SessionLocal() as db:
            rows = (
                db.query(EvolutionAgentProfile)
                .filter(EvolutionAgentProfile.is_active.is_(True))
                .order_by(EvolutionAgentProfile.updated_at.desc())
                .all()
            )
            profiles = [profile_from_row(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent-profile] load failed, using built-in: %s", exc)
        return agent_profile.builtin_profile()

    chosen = pick_profile(
        profiles, task_type=task_type, user_id=user_id, tenant_id=tenant_id
    )
    if chosen is None:
        return agent_profile.builtin_profile()
    ok, problems = agent_profile.validate_profile(chosen)
    if not ok:
        # A stored profile that no longer validates is not applied. The
        # alternative — applying it partially — produces a configuration nobody
        # reviewed and nobody can reproduce.
        logger.warning(
            "[agent-profile] stored profile %s rejected at load: %s",
            chosen.profile_id,
            problems,
        )
        return agent_profile.builtin_profile()
    return chosen
