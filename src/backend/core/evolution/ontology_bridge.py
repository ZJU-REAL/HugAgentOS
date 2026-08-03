"""Bridge ontology drafts into the shared candidate ledger (GCE ticket 22).

Ontology governance is already the most mature safe-evolution path in this
codebase: runtime evidence can only create a draft, an approved draft only
materialises as an *inactive* version, and an administrator must activate it
explicitly.

So this bridge deliberately flows in one direction.  Ontology adopts the shared
ledger — the same candidate list, evidence view and version lineage as every
other asset — and gives up **nothing**.  The unified framework inherits
ontology's discipline; ontology does not inherit the framework's faster paths.

Concretely, and non-negotiably:

* an ontology candidate never activates automatically, whatever the evidence;
* a new constraint runs in log-only mode first, so a false positive shows up as
  an observation rather than as blocked business;
* a hard constraint cannot be weakened by a low-risk candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MODE_LOG_ONLY = "log_only"
MODE_ENFORCE = "enforce"

# How long a new constraint must be observed before it may enforce, and the
# false-positive rate above which it is recommended for withdrawal instead.
MIN_LOG_ONLY_OBSERVATIONS = 20
MAX_FALSE_POSITIVE_RATE = 0.10


@dataclass
class OntologyCandidate:
    """An ontology draft expressed in the shared envelope."""

    draft_id: str
    pack_id: str
    operation: str
    evidence_event_ids: List[str] = field(default_factory=list)
    user_corrections: int = 0
    denial_count: int = 0
    proposal: Dict[str, Any] = field(default_factory=dict)
    mode: str = MODE_LOG_ONLY

    @property
    def auto_activatable(self) -> bool:
        """Always false. Present as executable documentation of the invariant."""
        return False

    def to_ir_payload(self) -> Dict[str, Any]:
        return {
            "target_kind": "ontology",
            "target_asset_id": self.pack_id,
            "operation": self.operation,
            "changes": [self.proposal],
            "evidence_refs": list(self.evidence_event_ids),
            # Highest tier by construction: ontology binds every other asset, so
            # a mistake here is not contained to one skill.
            "risk_tier": "high",
            "mode": self.mode,
        }


@dataclass
class ConsistencyProblem:
    code: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


def check_consistency(
    candidate: OntologyCandidate,
    *,
    known_concepts: Sequence[str],
    referenced_tools: Sequence[str],
    existing_workflows: Sequence[str],
    hard_constraints: Sequence[str] = (),
) -> Tuple[bool, List[ConsistencyProblem]]:
    """Structural checks before a draft may enter the ledger.

    A broken pack degrades every asset that depends on it, so these run before
    a human is asked to look at anything.
    """
    problems: List[ConsistencyProblem] = []
    proposal = candidate.proposal or {}

    concept = str(proposal.get("concept") or "")
    if concept and concept not in known_concepts:
        problems.append(
            ConsistencyProblem("dangling_concept", f"引用了不存在的概念 {concept}")
        )

    for tool in proposal.get("tools") or []:
        if tool not in referenced_tools:
            problems.append(
                ConsistencyProblem("unknown_tool", f"引用了不存在的工具 {tool}")
            )

    for workflow in proposal.get("breaks_workflows") or []:
        if workflow in existing_workflows:
            problems.append(
                ConsistencyProblem("breaks_workflow", f"会破坏既有工作流 {workflow}")
            )

    weakened = proposal.get("weakens_constraint")
    if weakened and weakened in hard_constraints:
        problems.append(
            ConsistencyProblem(
                "hard_constraint_weakened",
                f"试图弱化硬约束 {weakened}——任何候选都不允许",
            )
        )

    return (not problems), problems


def aggregate_false_positive_evidence(
    *,
    denials: Sequence[Dict[str, Any]],
    corrections: Sequence[Dict[str, Any]],
    min_denials: int = 2,
) -> Optional[Dict[str, Any]]:
    """Bundle repeated denials plus user corrections into one evidence packet.

    Requires *both* signals. Repeated denials alone may simply mean the rule is
    working; it is the human corrections that turn "the gate fired a lot" into
    "the gate is wrong".
    """
    by_rule: Dict[str, List[Dict[str, Any]]] = {}
    for denial in denials:
        rule_id = str(denial.get("rule_id") or "")
        if rule_id:
            by_rule.setdefault(rule_id, []).append(denial)

    for rule_id, items in by_rule.items():
        if len(items) < min_denials:
            continue
        related = [
            c for c in corrections if str(c.get("rule_id") or "") == rule_id
        ]
        if not related:
            continue
        return {
            "rule_id": rule_id,
            "denial_count": len(items),
            "correction_count": len(related),
            "event_ids": [str(i.get("event_id") or "") for i in items],
        }
    return None


def promote_to_enforce(
    *, observations: int, false_positives: int
) -> Tuple[bool, str]:
    """Whether a log-only constraint has earned the right to enforce.

    Below the observation floor there is no basis for the decision; above the
    false-positive ceiling the recommendation is withdrawal, not enforcement.
    """
    if observations < MIN_LOG_ONLY_OBSERVATIONS:
        return False, f"observations {observations} < {MIN_LOG_ONLY_OBSERVATIONS}"
    rate = false_positives / observations if observations else 1.0
    if rate > MAX_FALSE_POSITIVE_RATE:
        return False, f"false_positive_rate {rate:.2%} > {MAX_FALSE_POSITIVE_RATE:.0%} — 建议撤回"
    return True, ""


def activation_is_permitted(candidate: OntologyCandidate, *, approver: Optional[str]) -> Tuple[bool, str]:
    """Ontology activation always needs an explicit human act.

    There is no evidence threshold that unlocks automation here. That is the
    invariant the rest of the framework inherits, not an exception to it.
    """
    if candidate.auto_activatable:  # pragma: no cover - defensive, always False
        return False, "auto_activation_forbidden"
    if not approver:
        return False, "human_approval_required"
    return True, ""
