"""Cluster episodes into recurring patterns."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence
from core.evolution.cycle import promotion_constants

@dataclass
class Pattern:
    """A recurring shape found across episodes."""

    kind: str
    signature: str
    support: int
    episode_ids: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    tool_sequence: List[str] = field(default_factory=list)
    skill_sequence: List[str] = field(default_factory=list)
    memory_refs: List[str] = field(default_factory=list)
    single_shot: bool = False

    @property
    def meets_support(self) -> bool:
        return self.support >= promotion_constants.MIN_SUPPORT or self.single_shot

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "support": self.support,
            "success_rate": round(self.success_rate, 3),
            "tool_sequence": self.tool_sequence,
            "skill_sequence": self.skill_sequence,
            "memory_refs": self.memory_refs,
            "single_shot": self.single_shot,
            "episode_ids": self.episode_ids[:20],
        }


def pattern_signature(items: Sequence[str]) -> str:
    return "→".join(items)


def discover_patterns(episodes: List[Dict[str, Any]]) -> List[Pattern]:
    """Cluster episodes into recurring shapes.

    ``episodes`` are plain dicts so this stays testable without a database and
    usable over backfilled history (which has no asset bundle and therefore
    cannot be replayed, but is perfectly good for finding patterns).
    """
    by_tool_seq: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_skill_seq: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for episode in episodes:
        tools = [str(t) for t in (episode.get("tool_sequence") or [])]
        skills = [str(s) for s in (episode.get("skill_sequence") or [])]
        if tools:
            by_tool_seq[pattern_signature(tools)].append(episode)
        if len(skills) >= 2:
            by_skill_seq[pattern_signature(skills)].append(episode)

    patterns: List[Pattern] = []

    for signature, group in by_tool_seq.items():
        successes = [e for e in group if e.get("verdict") == "success"]
        failures = [e for e in group if e.get("verdict") == "failed"]
        rate = len(successes) / len(group) if group else 0.0

        if len(successes) >= promotion_constants.MIN_SUPPORT and rate >= promotion_constants.SKILL_MIN_SUCCESS_RATE:
            patterns.append(
                Pattern(
                    kind=promotion_constants.PATTERN_SUCCESS_SUBSEQUENCE,
                    signature=signature,
                    support=len(successes),
                    episode_ids=[e.get("episode_id", "") for e in successes],
                    success_rate=rate,
                    tool_sequence=signature.split("→"),
                    memory_refs=sorted(
                        {
                            ref
                            for e in successes
                            for ref in (e.get("memory_refs") or [])
                        }
                    ),
                )
            )
        if len(failures) >= promotion_constants.MIN_SUPPORT:
            patterns.append(
                Pattern(
                    kind=promotion_constants.PATTERN_REPEATED_FAILURE,
                    signature=signature,
                    support=len(failures),
                    episode_ids=[e.get("episode_id", "") for e in failures],
                    success_rate=rate,
                    tool_sequence=signature.split("→"),
                )
            )

    for signature, group in by_skill_seq.items():
        if len(group) >= promotion_constants.WORKFLOW_MIN_COMBO_FREQUENCY:
            successes = [e for e in group if e.get("verdict") == "success"]
            patterns.append(
                Pattern(
                    kind=promotion_constants.PATTERN_SUCCESS_SUBSEQUENCE,
                    signature=signature,
                    support=len(group),
                    episode_ids=[e.get("episode_id", "") for e in group],
                    success_rate=len(successes) / len(group),
                    skill_sequence=signature.split("→"),
                )
            )

    return [p for p in patterns if p.meets_support]
