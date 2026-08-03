"""Per-user evolution preferences.

Fine-grained because deployments differ more than a single on/off can express.
The ontology setting is the clearest case: some organisations run several domain
packs, some run exactly one, and plenty run none at all.  A design that assumes a
pack exists fails the third group entirely — every candidate would be rejected
for referencing concepts no pack defines, and evolution would appear broken
rather than simply unconfigured.

So ontology binding has three modes, and "none" is a first-class choice rather
than a degraded state:

* ``none``     — do not check candidates against any domain pack
* ``any``      — check against every active pack
* ``specific`` — check against one named pack

Stored on the user record next to the memory switches, so one place governs
what the loop does on this person's behalf.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)

PREFS_KEY = "evolution_prefs"

ONTOLOGY_NONE = "none"
ONTOLOGY_ANY = "any"
ONTOLOGY_SPECIFIC = "specific"
ONTOLOGY_MODES = (ONTOLOGY_NONE, ONTOLOGY_ANY, ONTOLOGY_SPECIFIC)

# Which mechanisms this user lets the loop propose for them.
MECH_MEMORY = "memory"
MECH_SKILL = "skill"
MECH_WORKFLOW = "workflow"
ALL_MECHANISMS = (MECH_MEMORY, MECH_SKILL, MECH_WORKFLOW)

DEFAULTS: Dict[str, Any] = {
    # Off until somebody says otherwise. Evolution reads a person's whole
    # episode history and writes back skills, prompt fragments and memory, so
    # defaulting it on would run all of that before the user was ever asked.
    # The first-run wizard is where consent is collected; a deployment that
    # skips the wizard therefore evolves nobody, which is the safe direction to
    # be wrong in.
    "enabled": False,
    # Defaults to "none" deliberately: assuming a domain pack exists is what
    # breaks deployments that have none, and the safe default is the one that
    # works everywhere.
    "ontology_mode": ONTOLOGY_NONE,
    "ontology_pack_id": "",
    "mechanisms": list(ALL_MECHANISMS),
    # Minimum times a pattern must recur before it may become a candidate.
    "min_support": 3,
    # Accept low-risk personal candidates without an explicit click.
    "auto_approve_low_risk": False,
}


def normalise(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fill in defaults and reject impossible combinations.

    A stored value that no longer makes sense — ``specific`` with no pack id,
    say, after the pack was deleted — degrades to ``none`` rather than causing
    every candidate to be rejected for an unresolvable reference.
    """
    prefs = {**DEFAULTS, **(raw or {})}

    mode = str(prefs.get("ontology_mode") or ONTOLOGY_NONE)
    if mode not in ONTOLOGY_MODES:
        mode = ONTOLOGY_NONE
    if mode == ONTOLOGY_SPECIFIC and not str(prefs.get("ontology_pack_id") or "").strip():
        mode = ONTOLOGY_NONE
    prefs["ontology_mode"] = mode
    if mode != ONTOLOGY_SPECIFIC:
        prefs["ontology_pack_id"] = ""

    mechanisms = [m for m in (prefs.get("mechanisms") or []) if m in ALL_MECHANISMS]
    prefs["mechanisms"] = mechanisms or list(ALL_MECHANISMS)

    # Explicit None check rather than `or`: a stored 0 is a value the user set,
    # not an absent one, and `or` would silently rewrite it to the default
    # instead of clamping it to the floor.
    raw_support = prefs.get("min_support")
    try:
        prefs["min_support"] = max(2, min(20, int(3 if raw_support is None else raw_support)))
    except (TypeError, ValueError):
        prefs["min_support"] = 3

    prefs["enabled"] = bool(prefs.get("enabled", False))
    prefs["auto_approve_low_risk"] = bool(prefs.get("auto_approve_low_risk", False))
    return prefs


def resolve_prefs(user_id: str) -> Dict[str, Any]:
    """Read a user's preferences, falling back to defaults. Never raises."""
    if not user_id:
        return normalise(None)
    try:
        from core.db.models import UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
            metadata = (getattr(row, "extra_data", None) or {}) if row else {}
        return normalise(metadata.get(PREFS_KEY))
    except Exception as exc:
        logger.debug("[evo-prefs] read failed for %s: %s", user_id, exc)
        return normalise(None)


def update_prefs(user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a partial update and persist it."""
    from core.db.models import UserShadow

    with SessionLocal() as db:
        row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
        if row is None:
            raise ValueError(f"user not found: {user_id}")
        metadata = dict(getattr(row, "extra_data", None) or {})
        merged = normalise({**(metadata.get(PREFS_KEY) or {}), **(patch or {})})
        metadata[PREFS_KEY] = merged
        row.extra_data = metadata
        db.commit()
    return merged


def available_ontology_packs() -> List[Dict[str, str]]:
    """Domain packs a user can bind to.

    Returns an empty list when none are configured — which the UI must present
    as "no packs available" rather than hiding the setting, so the absence is
    visible instead of looking like a missing feature.
    """
    try:
        from core.db.models import OntologyPack

        with SessionLocal() as db:
            return [
                {
                    "pack_id": p.pack_id,
                    "name": getattr(p, "display_name", None) or p.pack_id,
                    "active": bool(getattr(p, "active_version_id", None)),
                }
                for p in db.query(OntologyPack).all()
            ]
    except Exception as exc:
        logger.debug("[evo-prefs] pack listing failed: %s", exc)
        return []


def mechanism_enabled(prefs: Dict[str, Any], mechanism: str) -> bool:
    return mechanism in (prefs or {}).get("mechanisms", ALL_MECHANISMS)
