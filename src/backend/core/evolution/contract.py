"""Types shared across every layer of the evolution loop (GCE ticket 03).

The single most important idea here is the **asset bundle**: the exact set of
versions a run used.  Without it, "this run got worse" is unanswerable — you
cannot tell whether the memory policy changed, a skill was republished mid-run,
or the domain pack moved underneath you.  With it, any historical run can be
reconstructed and, crucially, replayed against a *single* substituted version so
attribution has something causal to stand on.

Bundles are immutable by construction: an admin publishing a new version while a
run is in flight must not change what that run is doing.  The bundle is captured
once, at run start, and referenced by id from then on.

The field shape deliberately echoes the agent-marketplace submission snapshot
(model config + skill/mcp/plugin/kb ids), which already solved "capture what
this agent was configured with" for a different reason.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.immutable import FrozenDict, freeze_json, thaw_json

# The four governed asset classes, plus the ones that turned out to matter in a
# multi-provider / multi-KB deployment. Prompt is included because this project
# already versions prompts with an activation pointer, so it is the cheapest
# asset to bring under release control.
AssetKind = str

ASSET_MEMORY: AssetKind = "memory"
ASSET_SKILL: AssetKind = "skill"
ASSET_WORKFLOW: AssetKind = "workflow"
ASSET_ONTOLOGY: AssetKind = "ontology"
ASSET_PROMPT: AssetKind = "prompt"
ASSET_MODEL: AssetKind = "model"
ASSET_KB: AssetKind = "kb"

ASSET_KINDS: Tuple[AssetKind, ...] = (
    ASSET_MEMORY,
    ASSET_SKILL,
    ASSET_WORKFLOW,
    ASSET_ONTOLOGY,
    ASSET_PROMPT,
    ASSET_MODEL,
    ASSET_KB,
)


@dataclass(frozen=True)
class AssetRef:
    """A pinned reference to one version of one asset."""

    kind: AssetKind
    asset_id: str
    version: str = ""
    # Free-form provenance, e.g. {"source": "filesystem"} for built-in skills
    # that have no database row and therefore no real version string.
    detail: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", freeze_json(self.detail or {}))

    def key(self) -> str:
        return f"{self.kind}:{self.asset_id}:{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        payload = {"kind": self.kind, "asset_id": self.asset_id, "version": self.version}
        if self.detail:
            payload["detail"] = thaw_json(self.detail)
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AssetRef":
        return cls(
            kind=str(raw.get("kind") or ""),
            asset_id=str(raw.get("asset_id") or ""),
            version=str(raw.get("version") or ""),
            detail=raw.get("detail") or {},
        )


@dataclass(frozen=True)
class AssetBundle:
    """Everything one run was pinned to, as an immutable snapshot.

    ``bundle_id`` is derived from the content, so two runs with an identical
    configuration share an id.  That is intentional: it makes "did anything
    change between these two runs?" a string comparison instead of a diff, which
    is the question replay and attribution ask constantly.
    """

    bundle_id: str
    refs: Tuple[AssetRef, ...] = ()
    # True when the bundle could not be fully resolved (e.g. backfilled from
    # historical logs). Such bundles are usable for pattern mining but MUST be
    # refused by counterfactual replay — replay's whole premise is "everything
    # else is frozen", and here we do not know what everything else was.
    partial: bool = False
    captured_at: Optional[str] = None
    # Sanitized, content-addressed execution evidence. It contains only hashes,
    # public references and size/count metadata — never prompt/project plaintext.
    execution_manifest: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", tuple(self.refs))
        object.__setattr__(
            self,
            "execution_manifest",
            freeze_json(self.execution_manifest or {}),
        )

    def of_kind(self, kind: AssetKind) -> List[AssetRef]:
        return [ref for ref in self.refs if ref.kind == kind]

    def first_of_kind(self, kind: AssetKind) -> Optional[AssetRef]:
        refs = self.of_kind(kind)
        return refs[0] if refs else None

    def replace_ref(self, new_ref: AssetRef) -> "AssetBundle":
        """Return a bundle with exactly one asset swapped.

        This is the primitive counterfactual replay is built on: change one
        thing, hold everything else fixed. Any other kind of substitution makes
        the resulting attribution uninterpretable.
        """
        replaced = False
        refs: List[AssetRef] = []
        for ref in self.refs:
            if ref.kind == new_ref.kind and ref.asset_id == new_ref.asset_id:
                refs.append(new_ref)
                replaced = True
            else:
                refs.append(ref)
        if not replaced:
            refs.append(new_ref)
        # A substituted asset is a *candidate* replay input, not evidence that
        # the replacement was actually rendered/executed. Keeping the original
        # request manifest would create a contradictory bundle that still looks
        # replay-eligible. The replay executor must materialize and attach a new
        # manifest before this derived bundle can become complete again.
        invalidated = bool(self.execution_manifest)
        return build_bundle(
            refs,
            partial=bool(self.partial or invalidated),
            captured_at=self.captured_at,
            execution_manifest=None if invalidated else self.execution_manifest,
        )

    def without(self, kind: AssetKind, asset_id: str) -> "AssetBundle":
        """Return a bundle with one asset removed.

        Removal is a distinct counterfactual from substitution — "what if this
        memory had never been injected" is a different question from "what if it
        had said something else", and attribution needs to ask both.
        """
        refs = [ref for ref in self.refs if not (ref.kind == kind and ref.asset_id == asset_id)]
        invalidated = bool(self.execution_manifest)
        return build_bundle(
            refs,
            partial=bool(self.partial or invalidated),
            captured_at=self.captured_at,
            execution_manifest=None if invalidated else self.execution_manifest,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "bundle_id": self.bundle_id,
            "partial": self.partial,
            "captured_at": self.captured_at,
            "refs": [ref.to_dict() for ref in self.refs],
        }
        if self.execution_manifest:
            payload["execution_manifest"] = thaw_json(self.execution_manifest)
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AssetBundle":
        refs = tuple(AssetRef.from_dict(r) for r in (raw.get("refs") or []))
        return cls(
            bundle_id=str(raw.get("bundle_id") or ""),
            refs=refs,
            partial=bool(raw.get("partial")),
            captured_at=raw.get("captured_at"),
            execution_manifest=raw.get("execution_manifest") or {},
        )

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for ref in self.refs:
            counts[ref.kind] = counts.get(ref.kind, 0) + 1
        return counts


def compute_bundle_id(
    refs: List[AssetRef], execution_manifest: Optional[Mapping[str, Any]] = None
) -> str:
    """Content-addressed bundle id.

    Sorted so ordering noise (which skill the loader happened to list first)
    never produces a spurious "the configuration changed".
    """
    keys = sorted(ref.key() for ref in refs)
    manifest_hash = str((execution_manifest or {}).get("aggregate_hash") or "")
    if manifest_hash:
        keys.append(f"manifest:{manifest_hash}")
    digest = hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:24]
    return f"bundle_{digest}"


def build_bundle(
    refs: List[AssetRef],
    *,
    partial: bool = False,
    captured_at: Optional[str] = None,
    execution_manifest: Optional[Mapping[str, Any]] = None,
) -> AssetBundle:
    """Assemble a bundle, de-duplicating identical refs."""
    seen: Dict[str, AssetRef] = {}
    for ref in refs:
        if not ref.asset_id:
            continue
        seen.setdefault(ref.key(), ref)
    ordered = sorted(seen.values(), key=lambda r: (r.kind, r.asset_id, r.version))
    return AssetBundle(
        bundle_id=compute_bundle_id(ordered, execution_manifest),
        refs=tuple(ordered),
        partial=partial,
        captured_at=captured_at,
        execution_manifest=execution_manifest or {},
    )


EMPTY_BUNDLE = AssetBundle(bundle_id="bundle_empty", refs=(), partial=True)


def bundle_checksum(bundle: AssetBundle) -> str:
    """Stable checksum over the full serialized bundle, for audit records."""
    payload = json.dumps(bundle.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def as_json(bundle: AssetBundle) -> str:
    return json.dumps(bundle.to_dict(), sort_keys=True, ensure_ascii=False)


__all__ = [
    "ASSET_KINDS",
    "ASSET_KB",
    "ASSET_MEMORY",
    "ASSET_MODEL",
    "ASSET_ONTOLOGY",
    "ASSET_PROMPT",
    "ASSET_SKILL",
    "ASSET_WORKFLOW",
    "AssetBundle",
    "AssetKind",
    "AssetRef",
    "EMPTY_BUNDLE",
    "as_json",
    "build_bundle",
    "bundle_checksum",
    "compute_bundle_id",
]
