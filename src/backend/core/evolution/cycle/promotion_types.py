"""Promotion proposals, negative-transfer records and ordering constraints."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

@dataclass
class PromotionProposal:
    """A promotion the chain wants to make, before it becomes an IR candidate."""

    from_layer: str
    to_layer: str
    signature: str
    support: int
    rationale: str
    source_refs: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    # Set when an equivalent asset already exists: promotion becomes a patch
    # rather than a new asset, so the library does not fill with near-duplicates.
    patch_target: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "signature": self.signature,
            "support": self.support,
            "rationale": self.rationale,
            "source_refs": list(self.source_refs),
            "payload": self.payload,
            "patch_target": self.patch_target,
        }


@dataclass
class NegativeTransfer:
    asset_id: str
    task_type: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "task_type": self.task_type,
            "before": round(self.before, 4),
            "after": round(self.after, 4),
            "delta": round(self.delta, 4),
        }


@dataclass
class OrderingConstraint:
    """A pairwise ordering that held across the supporting evidence.

    Carries the task types it was *validated in* and any it was *contradicted
    in*. That distinction is what lets a genuinely context-dependent ordering be
    used where it holds instead of being discarded everywhere.
    """

    before: str
    after: str
    support: int
    contexts: int  # distinct tool-set shapes it was observed in
    validated_in: Tuple[str, ...] = ()
    contradicted_in: Tuple[str, ...] = ()

    def applies_to(self, tools: Sequence[str], task_type: Optional[str] = None) -> bool:
        if self.before not in tools or self.after not in tools:
            return False
        if task_type is None:
            # No context given: only a rule contradicted nowhere may be used.
            return not self.contradicted_in
        if task_type in self.contradicted_in:
            return False
        if task_type in self.validated_in:
            return True
        # An unseen context. Extending there is only safe for a rule that has
        # never been contradicted; one known to flip somewhere is precisely the
        # kind that should not be guessed at.
        return not self.contradicted_in

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "support": self.support,
            "contexts": self.contexts,
            "validated_in": list(self.validated_in),
            "contradicted_in": list(self.contradicted_in),
        }
