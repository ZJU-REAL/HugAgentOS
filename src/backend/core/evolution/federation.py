"""Candidate-only cross-tenant evolution (GCE ticket 46).

Several tenants — and desktop instances running locally — can make the system
better together while **raw traces never leave their domain**.  Only redacted
candidates and aggregate metrics move.

The constraint came from privacy and compliance (local-mode traces contain local
paths, local commands and customer data), but it is also the design's second
genuinely novel axis: most work on sharing experience between instances assumes
traces can be centrally collected, and the "candidates only" setting is much
less studied while having an obvious reason to exist.

Two properties hold throughout:

* **The centre cannot compel.**  It aggregates and offers; every domain still
  replays and approves locally before anything activates. A centre that could
  activate remotely would be a fleet-wide remote-code-execution path.
* **Contributors stay unidentifiable.**  Candidates below a support threshold
  are not distributed at all, so a distributed candidate cannot be traced back
  to the single tenant it came from.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Below this many independent contributing domains, a candidate stays home:
# distributing a candidate only one tenant produced would effectively publish
# that tenant's business specifics.
MIN_CONTRIBUTING_DOMAINS = 3

# Shapes that betray a local environment. Their presence blocks upload outright
# rather than being scrubbed — a redaction that has to guess is a redaction that
# eventually misses.
_LOCAL_MARKERS = (
    re.compile(r"[A-Za-z]:\\\\"),          # Windows drive path
    re.compile(r"/Users/[^/\s]+"),          # macOS home
    re.compile(r"/home/[^/\s]+"),           # Linux home
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # bare IP
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]"),
)


@dataclass
class ShareableCandidate:
    """What may cross a domain boundary."""

    signature: str
    target_kind: str
    operation: str
    changes: List[Dict[str, Any]] = field(default_factory=list)
    support: int = 0
    aggregate_metrics: Dict[str, float] = field(default_factory=dict)
    origin_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "target_kind": self.target_kind,
            "operation": self.operation,
            "changes": self.changes,
            "support": self.support,
            "aggregate_metrics": self.aggregate_metrics,
            # A one-way hash: lets the centre count distinct contributors
            # without learning who they are.
            "origin_hash": self.origin_hash,
        }


class UploadRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def origin_hash(domain_id: str, salt: str = "gce") -> str:
    return hashlib.sha256(f"{salt}:{domain_id}".encode("utf-8")).hexdigest()[:16]


def scan_for_local_markers(payload: Any) -> List[str]:
    """Find anything that would leak a local environment."""
    text = str(payload)
    return [pattern.pattern for pattern in _LOCAL_MARKERS if pattern.search(text)]


def prepare_for_upload(
    *,
    signature: str,
    target_kind: str,
    operation: str,
    changes: Sequence[Dict[str, Any]],
    metrics: Dict[str, float],
    domain_id: str,
    upload_enabled: bool,
    raw_trace: Any = None,
) -> ShareableCandidate:
    """Build the uploadable form, refusing anything that must not travel."""
    if not upload_enabled:
        raise UploadRefused("upload_disabled", "该域未开启候选上传")

    if raw_trace is not None:
        # Raw traces never leave, full stop. Accepting one "just this once"
        # would make the guarantee untestable.
        raise UploadRefused("raw_trace_never_uploaded", "原始轨迹一律不上传")

    markers = scan_for_local_markers(changes)
    if markers:
        raise UploadRefused(
            "local_markers_present",
            f"候选包含本地环境痕迹，拒绝上传: {markers}",
        )

    return ShareableCandidate(
        signature=signature,
        target_kind=target_kind,
        operation=operation,
        changes=list(changes),
        support=1,
        aggregate_metrics=dict(metrics),
        origin_hash=origin_hash(domain_id),
    )


@dataclass
class AggregatedCandidate:
    signature: str
    target_kind: str
    operation: str
    changes: List[Dict[str, Any]] = field(default_factory=list)
    contributing_domains: int = 0
    mean_effect: float = 0.0

    @property
    def distributable(self) -> bool:
        return self.contributing_domains >= MIN_CONTRIBUTING_DOMAINS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "target_kind": self.target_kind,
            "operation": self.operation,
            "contributing_domains": self.contributing_domains,
            "mean_effect": round(self.mean_effect, 4),
            "distributable": self.distributable,
        }


def aggregate(candidates: Sequence[ShareableCandidate]) -> List[AggregatedCandidate]:
    """Group equivalent candidates and count distinct contributors."""
    grouped: Dict[str, List[ShareableCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.signature, []).append(candidate)

    out: List[AggregatedCandidate] = []
    for signature, group in grouped.items():
        domains = {c.origin_hash for c in group if c.origin_hash}
        effects = [c.aggregate_metrics.get("effect_size", 0.0) for c in group]
        out.append(
            AggregatedCandidate(
                signature=signature,
                target_kind=group[0].target_kind,
                operation=group[0].operation,
                changes=group[0].changes,
                contributing_domains=len(domains),
                mean_effect=sum(effects) / len(effects) if effects else 0.0,
            )
        )
    out.sort(key=lambda c: (c.contributing_domains, c.mean_effect), reverse=True)
    return out


def distributable(candidates: Sequence[AggregatedCandidate]) -> List[AggregatedCandidate]:
    """Only sufficiently-supported candidates go back out."""
    return [c for c in candidates if c.distributable]


def accept_downstream(
    candidate: AggregatedCandidate,
    *,
    download_enabled: bool,
) -> Dict[str, Any]:
    """What a receiving domain does with an offered candidate.

    It arrives as a *draft*. Local replay and local approval are still required —
    the centre offers, it never activates.
    """
    if not download_enabled:
        return {"accepted": False, "reason": "download_disabled"}
    return {
        "accepted": True,
        "status": "draft",
        "requires_local_replay": True,
        "requires_local_approval": True,
        "source": "federation",
    }


def centre_can_activate() -> bool:
    """Always false.

    Written as a function so the property is executable rather than a comment:
    a centre able to activate remotely would be a fleet-wide remote code
    execution path.
    """
    return False


@dataclass
class FederationAudit:
    uploaded: List[str] = field(default_factory=list)
    received: List[str] = field(default_factory=list)
    activated: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uploaded": list(self.uploaded),
            "received": list(self.received),
            "activated": list(self.activated),
        }
