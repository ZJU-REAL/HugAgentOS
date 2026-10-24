"""skills responsibilities."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from features.desktop.capabilities import registry
from core.capabilities.identity_rules import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE
from features.desktop.capabilities.resolver import SOURCE_BUILTIN, SOURCE_CLOUD, SOURCE_LOCAL, Candidate, Resolution, resolve
import logging
logger = logging.getLogger(__name__)
from features.desktop.capabilities import skill_account
from features.desktop.capabilities import skill_candidates
from features.desktop.capabilities import skills_state

def view_generation() -> int:
    """Bumped whenever store contents change; view consumers refresh on a new value."""
    return skills_state.view_generation


def bump_view_generation() -> int:
    pass  # State is owned by its responsibility module.
    with skills_state.resolution_lock:
        skills_state.view_generation += 1
        return skills_state.view_generation


def resolve_for_user(user_id: Optional[str], *, requested: Optional[Set[str]] = None) -> Resolution:
    """Which candidate wins each runtime name, recomputed only when it can differ.

    The answer is a pure function of the installations, the store contents, the
    user's name preferences and the bound cloud account — every one of which
    moves ``registry.generation()``, ``view_generation()`` or the account
    profile. Agent assembly asks for this several times per message and the
    eligibility fixpoint walks every candidate's package each time, so without
    this the same answer was recomputed from disk two or three times a turn.
    """
    from features.desktop.capabilities.discovery import scan
    from features.desktop.capabilities.readiness import eligible_skill_candidates
    scan(user_id)
    key = user_id or ""
    signal = (
        registry.generation(),
        view_generation(),
        skill_account.current_account_profile(),
        skill_candidates.builtin_signature(),
        frozenset(requested) if requested is not None else None,
    )
    with skills_state.resolution_lock:
        hit = skills_state.resolution_cache.get(key)
        if hit is not None and hit[0] == signal:
            skills_state.last_resolution[key] = hit[1]
            return hit[1]

    preferences = registry.preferences(KIND_SKILL, user_id=user_id)
    eligible = eligible_skill_candidates(
        skill_candidates.fill_hashes(skill_candidates.candidates(user_id)), user_id, preferences=preferences, requested=requested
    )
    res = resolve(KIND_SKILL, eligible, preferences=preferences, requested=requested)
    with skills_state.resolution_lock:
        skills_state.last_resolution[key] = res
        # A cloud deployment shares this process across users; keep the memo
        # bounded rather than letting it grow with the user table.
        if len(skills_state.resolution_cache) >= 64 and key not in skills_state.resolution_cache:
            skills_state.resolution_cache.pop(next(iter(skills_state.resolution_cache)), None)
        skills_state.resolution_cache[key] = (signal, res)
    return res


def filter_available_names(names, *, user_id=None):
    """Implicit defaults omit unusable names; explicit bindings still fail in preflight."""
    chosen = resolve_for_user(user_id).chosen
    return [name for name in dict.fromkeys(names) if name in chosen]


def last_resolution(user_id: Optional[str]) -> Optional[Resolution]:
    with skills_state.resolution_lock:
        return skills_state.last_resolution.get(user_id or "")


def info_candidate(info: Any) -> Candidate:
    """Map a backend ``SkillFileInfo`` onto a resolver candidate."""
    src = info.source_name
    origin = dict(getattr(info, "origin", None) or {})
    if src == "built-in":
        return Candidate(
            install_id=registry.install_id(KIND_SKILL, BUILTIN_PROFILE, info.skill_id),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=BUILTIN_PROFILE,
            source=SOURCE_BUILTIN,
            path=Path(info.file_path).parent,
            account_level=False,
        )
    if src in ("cloud", "device-store"):
        return Candidate(
            install_id=str(origin.get("install_id") or ""),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=str(origin.get("profile") or ""),
            source=SOURCE_CLOUD if src == "cloud" else SOURCE_LOCAL,
            path=Path(info.file_path).parent,
            content_hash=origin.get("content_hash"),
            revision=origin.get("revision"),
            account_level=True,
        )
    if src == "admin":
        # Device catalog row (created here, installed from a plugin or the market).
        return Candidate(
            install_id=registry.install_id(KIND_SKILL, LOCAL_PROFILE, info.skill_id),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=LOCAL_PROFILE,
            source=SOURCE_LOCAL,
            path=Path("<db>"),
            account_level=True,
        )
    return Candidate(
        install_id=registry.install_id(KIND_SKILL, src, info.skill_id),
        runtime_name=info.skill_id,
        kind=KIND_SKILL,
        profile=src,
        source=SOURCE_LOCAL,
        path=Path(info.file_path).parent,
        account_level=False,
    )


def merge_skill_infos(groups: Dict[str, List[Any]], *, backend_hash) -> Dict[str, Any]:
    """Composite-backend merge hook: one winner per id, conflicts load nothing.

    ``backend_hash(info) -> str`` computes a content hash for a backend entry; it
    is only called for ids with more than one candidate.
    """
    cands: List[Candidate] = []
    lookup: Dict[str, Any] = {}
    for skill_id, infos in groups.items():
        for info in infos:
            cand = info_candidate(info)
            if len(infos) > 1 and cand.content_hash is None:
                cand = Candidate(**{**cand.__dict__, "content_hash": backend_hash(info)})
            cands.append(cand)
            lookup[cand.install_id] = info
    res = resolve(
        KIND_SKILL,
        cands,
        preferences=registry.preferences(KIND_SKILL, user_id=skill_account.current_local_user_id()),
    )
    with skills_state.resolution_lock:
        skills_state.last_resolution["__loader__"] = res
    for name in res.conflicts:
        logger.warning("[caps] skill name conflict, nothing loaded for '%s'", name)
    return {name: lookup[c.install_id] for name, c in res.chosen.items()}


def loader_resolution() -> Optional[Resolution]:
    with skills_state.resolution_lock:
        return skills_state.last_resolution.get("__loader__")


def conflicted_names() -> Set[str]:
    res = loader_resolution()
    return set(res.conflicts) if res else set()
