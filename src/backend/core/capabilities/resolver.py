"""The one place that decides which candidate a runtime name points to.

Order, explicit to implicit, every step visible to the user:

1. the user's explicit name preference on this device;
2. an explicit request for one candidate in this run;
3. the current account's usable candidate, when it is the only one;
4. the only usable candidate overall.

Candidates whose content is byte-identical are not a conflict — the copy that is
cheapest to serve wins deterministically. Anything else with two usable
candidates is a ``name_conflict``: nothing is chosen and the model never sees
two same-named capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .paths import BUILTIN_PROFILE, LOCAL_PROFILE
from .ref import ResourceRef

SOURCE_CLOUD = "cloud"
SOURCE_LOCAL = "local"
SOURCE_BUILTIN = "builtin"
SOURCE_PLUGIN = "plugin"

_PROFILE_RANK = {LOCAL_PROFILE: 0, BUILTIN_PROFILE: 2}


@dataclass(frozen=True)
class Candidate:
    install_id: str
    runtime_name: str
    kind: str
    profile: str
    source: str
    path: Optional[Path] = None
    content_hash: Optional[str] = None
    revision: Optional[str] = None
    ref: Optional[ResourceRef] = None
    usable: bool = True
    account_level: bool = False
    state: str = "ready"
    display_name: str = ""
    description: str = ""
    version: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "install_id": self.install_id,
            "runtime_name": self.runtime_name,
            "kind": self.kind,
            "profile": self.profile,
            "source": self.source,
            "path": str(self.path) if self.path else None,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "resource_ref": self.ref.to_dict() if self.ref else None,
            "usable": self.usable,
            "account_level": self.account_level,
            "state": self.state,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
        }


@dataclass
class Resolution:
    chosen: Dict[str, Candidate] = field(default_factory=dict)
    shadowed: Dict[str, List[Candidate]] = field(default_factory=dict)
    conflicts: Dict[str, List[Candidate]] = field(default_factory=dict)
    unusable: Dict[str, List[Candidate]] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)

    def chosen_ids(self) -> Set[str]:
        return {c.install_id for c in self.chosen.values()}

    def to_dict(self) -> Dict[str, object]:
        return {
            "chosen": {n: c.to_dict() for n, c in self.chosen.items()},
            "shadowed": {n: [c.to_dict() for c in cs] for n, cs in self.shadowed.items()},
            "conflicts": {n: [c.to_dict() for c in cs] for n, cs in self.conflicts.items()},
            "unusable": {n: [c.to_dict() for c in cs] for n, cs in self.unusable.items()},
            "reasons": dict(self.reasons),
        }


def _rank(c: Candidate) -> tuple:
    return (0 if c.account_level else 1, _PROFILE_RANK.get(c.profile, 1), c.install_id)


def _identical(cands: List[Candidate]) -> bool:
    hashes = {c.content_hash for c in cands}
    return len(hashes) == 1 and None not in hashes


def _pick(
    name: str, group: List[Candidate], res: Resolution, winner: Candidate, reason: str
) -> None:
    res.chosen[name] = winner
    losers = [c for c in group if c.install_id != winner.install_id]
    if losers:
        res.shadowed[name] = losers
    res.reasons[name] = reason


def resolve(
    kind: str,
    candidates: Iterable[Candidate],
    *,
    preferences: Optional[Dict[str, str]] = None,
    requested: Optional[Set[str]] = None,
) -> Resolution:
    preferences = preferences or {}
    requested = requested or set()
    res = Resolution()
    groups: Dict[str, List[Candidate]] = {}
    for c in candidates:
        if c.kind != kind:
            raise ValueError(f"candidate {c.install_id} is {c.kind}, expected {kind}")
        groups.setdefault(c.runtime_name, []).append(c)

    for name, group in sorted(groups.items()):
        usable = [c for c in group if c.usable and c.path is not None]
        if not usable:
            res.unusable[name] = group
            res.reasons[name] = "no_usable_candidate"
            continue

        pref = preferences.get(name)
        if pref:
            match = [c for c in group if c.install_id == pref]
            if match:
                if match[0] in usable:
                    _pick(name, group, res, match[0], "preference")
                else:
                    # The user chose it; it is not ready. Nothing else may take the name.
                    res.unusable[name] = group
                    res.reasons[name] = "preferred_not_ready"
                continue

            # An explicit binding remains authoritative if its source was
            # removed or is temporarily absent from the current manifest.
            res.unusable[name] = group
            res.reasons[name] = "preferred_missing"
            continue

        requested_group = [c for c in group if c.install_id in requested]
        if requested_group and any(c not in usable for c in requested_group):
            res.unusable[name] = group
            res.reasons[name] = "requested_not_ready"
            continue
        asked = [c for c in usable if c.install_id in requested]
        if len(asked) == 1:
            _pick(name, group, res, asked[0], "requested")
            continue
        if len(asked) > 1:
            if _identical(asked):
                _pick(name, group, res, sorted(asked, key=_rank)[0], "requested_identical")
            else:
                res.conflicts[name] = group
                res.reasons[name] = "requested_conflict"
            continue

        account = [c for c in usable if c.account_level]
        if len(account) == 1:
            _pick(name, group, res, account[0], "account_unique")
            continue
        if len(account) > 1:
            if _identical(account):
                _pick(name, group, res, sorted(account, key=_rank)[0], "account_identical")
            else:
                res.conflicts[name] = group
                res.reasons[name] = "account_conflict"
            continue

        if len(usable) == 1:
            _pick(name, group, res, usable[0], "global_unique")
            continue
        if _identical(usable):
            _pick(name, group, res, sorted(usable, key=_rank)[0], "global_identical")
            continue
        res.conflicts[name] = group
        res.reasons[name] = "global_conflict"
    return res
