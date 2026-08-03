"""Per-user evolution participation, derived from the single evolution switch.

There used to be two user-facing switches: "start evolution" (does the loop
distil for me) and "contribute to evolution" (do my conversations feed
cross-user learning).  The distinction was real but nobody could tell the two
apart in the settings panel, so the product now has exactly one switch —
``evolution_prefs.enabled`` — and contribution follows it: evolution on means
the loop may distil for this user *and* their conversations may serve as
evidence within the tenant; off means neither.

Enforcement still lives at one point rather than being scattered: the
Episode's ``privacy_class``.  With the switch off, episodes are marked
``private`` and pattern mining filters those out — so the user still gets
their own in-chat card and their own history, while nothing of theirs feeds
cross-user learning. Anything weaker would make the promise unverifiable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)

# Response-field name kept from the era of a separate contribution switch, so
# older bundled frontends that read /evolution/settings keep working.
SETTING_KEY = "evolution_contribution_enabled"

# Episodes from an opted-out user. Recorded for that user's own benefit (their
# card, their history) but excluded from anything cross-user.
PRIVACY_PRIVATE = "private"
PRIVACY_TENANT = "tenant"


def is_contribution_enabled(metadata: Optional[Dict[str, Any]]) -> bool:
    """Whether this user's episodes may feed the evolution loop.

    Reads the single evolution switch (``evolution_prefs.enabled``), so one
    stored value governs both distilling *for* the user and learning *from*
    them. Off by default: consent is collected by the first-run wizard, and a
    user who never enabled evolution contributes nothing. "On" only ever means
    *within the tenant*: cross-tenant sharing is a separate, independently-
    gated decision (see ``federation``).
    """
    from core.evolution.prefs import PREFS_KEY, normalise

    if not metadata:
        return False
    return bool(normalise(metadata.get(PREFS_KEY)).get("enabled", False))


def privacy_class_for(metadata: Optional[Dict[str, Any]]) -> str:
    """The Episode privacy class implied by the user's setting."""
    return PRIVACY_TENANT if is_contribution_enabled(metadata) else PRIVACY_PRIVATE


def resolve_for_user(user_id: str) -> Dict[str, Any]:
    """Read the setting for one user. Degrades to the default, never raises."""
    if not user_id:
        return {SETTING_KEY: False, "privacy_class": PRIVACY_PRIVATE}
    try:
        from core.db.models import UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
            metadata = (getattr(row, "extra_data", None) or {}) if row else {}
    except Exception as exc:
        logger.debug("[evo-settings] read failed for %s: %s", user_id, exc)
        metadata = {}

    enabled = is_contribution_enabled(metadata)
    return {SETTING_KEY: enabled, "privacy_class": privacy_class_for(metadata)}


def update_for_user(user_id: str, enabled: bool) -> Dict[str, Any]:
    """Persist the setting by writing the single evolution switch.

    Kept as an alias so older bundled frontends PATCHing /evolution/settings
    still work; it writes the same ``evolution_prefs.enabled`` the preferences
    panel writes, never a separate value that could drift from it.

    Turning it off does **not** retroactively delete already-contributed
    evidence — that would silently rewrite history other candidates were derived
    from. It stops future episodes from contributing; withdrawing past evidence
    is a separate, explicit action so its consequences stay visible.
    """
    from core.evolution.prefs import update_prefs

    merged = update_prefs(user_id, {"enabled": bool(enabled)})
    on = bool(merged.get("enabled", False))
    return {
        SETTING_KEY: on,
        "privacy_class": PRIVACY_TENANT if on else PRIVACY_PRIVATE,
        "note": "仅影响此后新产生的证据；已贡献的证据需单独撤回",
    }


# A candidate is the user's to approve when their own episodes account for at
# least this share of its evidence. Below it the capability was learned mostly
# from other people's work, and one person approving it for everyone is a
# different decision that belongs with an administrator.
OWN_EVIDENCE_THRESHOLD = 0.6


def personal_action(candidate) -> Optional[Dict[str, str]]:
    """What this user can actually do with a candidate, or ``None``.

    The queue used to list everything whose evidence was mostly the user's, and
    label every row 「为我启用」. Most of those rows could not be actioned at all:
    a memory candidate fell through to the skill materialiser and answered
    "暂不支持物化 memory 候选"; a medium/high-risk skill has to ramp through
    replay → shadow → canary and cannot be force-activated by one person; an
    orchestration profile changes a whole task type for everyone, so there is no
    personal version of it; and a *retirement* proposal offered a button to
    "enable" the thing it was arguing should be removed.

    Every one of those was a button whose only outcome was an error toast. A
    queue is a list of decisions the reader can make — so a candidate that
    cannot be actioned here does not belong here, and the ones that remain say
    what the button will do rather than all claiming to "enable".
    """
    kind = str(candidate.target_kind or "")
    operation = str(candidate.operation or "")
    risk = str(candidate.risk_tier or "medium")

    if kind == "memory":
        # The user's own memories: adjusting how they are retrieved is theirs to
        # decide, nothing is deleted, and every op records its undo.
        #
        # Only operations that have an executor are offered. The IR permits more
        # than `apply_memory_ops` can carry out — `deprecate` in particular is
        # proposable but refused at execution with `no_executor_for_operation` —
        # and offering one of those is the same defect as before: a button whose
        # only possible outcome is a refusal.
        labels = {
            "reweight": "调整这条记忆的权重",
            "merge": "合并这几条重复记忆",
        }
        if operation not in labels:
            return None
        return {
            "action": "apply_memory",
            "label": labels[operation],
            "effect": "只改变检索权重，不会删除记忆，可随时撤销",
        }

    if kind == "skill" and operation in ("new", "patch", "merge"):
        # Mirrors what `activation._entry_stage` will actually permit. A personal
        # activation skips the ramp because its blast radius is one consenting
        # user — but only where the candidate has already passed replay, or its
        # tier owes nothing beyond replay. Listing a medium-risk draft here put
        # a button on screen that answered "force 只允许用于 low 风险候选".
        from core.evolution.release import ACTIVE, REPLAY_PASSED

        replayed = str(candidate.status or "") in (REPLAY_PASSED, ACTIVE)
        if not replayed and risk != "low":
            return None
        return {
            "action": "install_skill",
            "label": "为我启用",
            "effect": "作为你的私有技能安装，只对你生效",
        }

    # skill/deprecate, agent_profile, prompt, subagent: fleet-wide decisions.
    return None


def _personally_activated_ids(db, user_id: str, candidate_ids: List[str]) -> set:
    """Candidates this user has already turned into a live personal release.

    A personal activation deliberately leaves the candidate's status untouched
    so others (and an administrator) can still act on it — which means status
    alone cannot tell "this user already accepted it". The release record can:
    it is scoped to the owner at activation. Terminal stages (rolled back /
    retired / rejected) do not count, so a user who undid a personal skill is
    offered it again rather than locked out.
    """
    if not candidate_ids:
        return set()
    out = set()
    try:
        from core.db.models.evolution import EvolutionRelease
        from core.evolution.release import TERMINAL_STAGES

        for release in (
            db.query(EvolutionRelease)
            .filter(EvolutionRelease.candidate_id.in_(candidate_ids))
            .all()
        ):
            scope = release.scope_filter or {}
            if str(scope.get("owner_user_id") or "") != user_id:
                continue
            if str(release.stage or "") in TERMINAL_STAGES:
                continue
            out.add(release.candidate_id)
    except Exception as exc:
        # Degrade to "exclude nothing": a broken release lookup must not empty
        # the whole queue, and re-offering an accepted candidate is the safer
        # failure (a repeat activation is idempotent in effect).
        logger.warning("[evo-settings] personal release lookup failed: %s", exc)
        db.rollback()
    return out


def pending_for_user(user_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Candidates this user may approve for themselves.

    Scoped deliberately. A skill distilled from someone's own conversations is
    reasonably theirs to accept, and requiring an administrator for it makes the
    loop unusable in practice. But a candidate built mostly from other people's
    evidence activates a capability for the fleet, and letting any single user
    push that is a governance hole wearing the costume of convenience — those
    stay with an administrator.
    """
    if not user_id:
        return []
    try:
        from core.db.models.evolution import EvolutionCandidate, EvolutionEpisode

        with SessionLocal() as db:
            own_ids = {
                row.episode_id
                for row in db.query(EvolutionEpisode.episode_id)
                .filter(EvolutionEpisode.user_id == user_id)
                .all()
            }
            if not own_ids:
                return []

            candidates = (
                db.query(EvolutionCandidate)
                .filter(EvolutionCandidate.status.in_(("draft", "replay_passed")))
                .order_by(EvolutionCandidate.created_at.desc())
                .limit(200)
                .all()
            )
            # A skill candidate the user already accepted stays draft/replay_passed
            # by design (its life continues for others), so without this it would
            # reappear on every refresh — an "enable" button for something already
            # enabled, whose second press just stamps out a duplicate release.
            already_mine = _personally_activated_ids(
                db, user_id, [c.candidate_id for c in candidates]
            )

            out: List[Dict[str, Any]] = []
            for candidate in candidates:
                if candidate.candidate_id in already_mine:
                    continue
                refs = set(candidate.evidence_refs or [])
                if not refs:
                    continue
                mine = refs & own_ids
                share = len(mine) / len(refs)
                if share < OWN_EVIDENCE_THRESHOLD:
                    continue
                action = personal_action(candidate)
                if action is None:
                    continue
                out.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "target_kind": candidate.target_kind,
                        "operation": candidate.operation,
                        "summary": (candidate.hypothesis or "")[:140],
                        "status": candidate.status,
                        "risk_tier": candidate.risk_tier,
                        "your_episodes": len(mine),
                        "total_evidence": len(refs),
                        "own_share": round(share, 3),
                        "tool_sequence": _tool_sequence_of_ir(candidate.ir),
                        # What the button does, and what it will change. Without
                        # these the row states a diagnosis and offers a verb that
                        # does not follow from it.
                        "action": action["action"],
                        "action_label": action["label"],
                        "action_effect": action["effect"],
                        "change": _change_preview(candidate),
                        "created_at": candidate.created_at.isoformat()
                        if candidate.created_at
                        else None,
                    }
                )
                if len(out) >= limit:
                    break
            return out
    except Exception as exc:
        logger.warning("[evo-settings] pending candidates failed: %s", exc)
        return []


def _change_preview(candidate) -> Dict[str, Any]:
    """The concrete content of the proposed change.

    A one-line hypothesis explains *why* something is proposed and says nothing
    about *what* it is. Approving a distilled skill without being able to read
    its body is not a decision, it is a guess — the text was already in the IR,
    it was simply never sent to the page that asks the user to accept it.
    """
    ir = candidate.ir or {}
    kind = str(candidate.target_kind or "")
    for change in ir.get("changes") or []:
        if kind == "skill":
            document = change.get("document")
            if isinstance(document, dict) and document.get("content"):
                return {
                    "type": "skill_document",
                    "display_name": str(document.get("display_name") or ""),
                    "description": str(document.get("description") or ""),
                    "allowed_tools": [str(t) for t in (document.get("allowed_tools") or [])],
                    "content": str(document.get("content") or ""),
                }
        if kind == "memory":
            operations = change.get("operations") or []
            if operations:
                return {
                    "type": "memory_ops",
                    "operations": [
                        {
                            "operation": str(op.get("operation") or ""),
                            "text": str(op.get("text") or op.get("memory_ref") or ""),
                            "reason": str(op.get("reason") or ""),
                            "before": op.get("before"),
                            "after": op.get("after"),
                        }
                        for op in operations
                    ],
                }
    return {}


def _tool_sequence_of_ir(ir: Optional[Dict[str, Any]]) -> List[str]:
    for change in (ir or {}).get("changes") or []:
        sequence = change.get("tool_sequence")
        if isinstance(sequence, list) and sequence:
            return [str(t) for t in sequence]
    return []


def approve_for_user(candidate_id: str, user_id: str) -> Dict[str, Any]:
    """Approve a candidate and activate it **for this user only**.

    The resulting skill is private to them, so accepting it cannot change what
    anyone else's agent does. That is what makes personal approval safe enough
    to sit in a settings panel rather than behind an administrator.
    """
    from core.evolution.activation import materialise_skill_candidate

    allowed = {c["candidate_id"]: c for c in pending_for_user(user_id, limit=200)}
    row = allowed.get(candidate_id)
    if row is None:
        # Distinguish "already yours" from "not yours to approve" — a stale tab
        # or double click lands here, and telling that user they lack permission
        # over a skill they just installed reads as a failure.
        with SessionLocal() as db:
            if candidate_id in _personally_activated_ids(db, user_id, [candidate_id]):
                raise PermissionError("该候选你已启用过，对应能力已在你的技能列表中，无需重复启用")
        raise PermissionError(
            "该候选无法由你单独批准：可能主要来自他人的对话证据，或属于影响全体成员的编排/退役变更，需管理员审批"
        )

    # Dispatch on what the candidate actually is. Sending everything to the
    # skill materialiser is what made memory candidates fail with
    # "暂不支持物化 memory 候选" on every click.
    if row["action"] == "apply_memory":
        from core.evolution.memory_apply import apply_memory_candidate

        result = apply_memory_candidate(
            candidate_id, approver=f"user:{user_id}", require_user_id=user_id
        )
    else:
        result = materialise_skill_candidate(
            candidate_id,
            approver=f"user:{user_id}",
            force=True,
            owner_user_id=user_id,
        )
    result["scope"] = "personal"
    return result


def summarise_contributions(user_id: str) -> Dict[str, Any]:
    """What this user's conversations have actually produced.

    Answers the question the settings panel raises: "I left it on — so what did
    it do?" Shows the chain from the user's own episodes through to candidates,
    with each candidate's true governance state rather than an optimistic one.
    """
    empty = {
        "episodes": 0,
        "contributed_episodes": 0,
        "private_episodes": 0,
        "candidates": [],
        "memory_written": 0,
    }
    if not user_id:
        return empty

    try:
        from core.db.models.evolution import (
            EvolutionCandidate,
            EvolutionEpisode,
            EvolutionTraceEvent,
        )

        with SessionLocal() as db:
            episodes = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.user_id == user_id)
                .order_by(EvolutionEpisode.created_at.desc())
                .limit(500)
                .all()
            )
            if not episodes:
                return empty

            episode_ids = {e.episode_id for e in episodes}
            contributed = [e for e in episodes if e.privacy_class != PRIVACY_PRIVATE]

            memory_written = 0
            for event in (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.episode_id.in_(list(episode_ids)))
                .all()
            ):
                if event.event_type == "memory.retrieved":
                    memory_written += int((event.payload or {}).get("injected") or 0)

            # Candidates whose evidence includes any of this user's episodes.
            candidates = []
            for candidate in (
                db.query(EvolutionCandidate)
                .order_by(EvolutionCandidate.created_at.desc())
                .limit(200)
                .all()
            ):
                refs = set(candidate.evidence_refs or [])
                shared = refs & episode_ids
                if not shared:
                    continue
                candidates.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "target_kind": candidate.target_kind,
                        "summary": (candidate.hypothesis or "")[:120],
                        # The real state — a draft says draft.
                        "status": candidate.status,
                        "risk_tier": candidate.risk_tier,
                        "your_episodes": len(shared),
                        "total_evidence": len(refs),
                        "created_at": candidate.created_at.isoformat()
                        if candidate.created_at
                        else None,
                    }
                )

            return {
                "episodes": len(episodes),
                "contributed_episodes": len(contributed),
                "private_episodes": len(episodes) - len(contributed),
                "candidates": candidates,
                "memory_written": memory_written,
            }
    except Exception as exc:
        logger.warning("[evo-settings] contribution summary failed: %s", exc)
        return empty
