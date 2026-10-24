"""Memory evolution operations (GCE ticket 21).

Memory should not only accumulate.  The motivating case: a profile says "prefers
slide decks", the user says "spreadsheet only, no deck", and the agent produces
a deck anyway.

The instinct is to delete the preference.  That is usually the wrong repair, and
distinguishing the two possibilities is the point of this module:

* the memory **content** is stale — the preference genuinely changed; or
* the **retrieval/injection policy** does not know that an explicit current
  instruction outranks a standing soft preference.

Most often it is the second. The retriever should still surface the preference;
it just has to be marked as *overridden by the current request* so it never
reaches the actionable-suggestion slot. So the fix is a policy change plus a
confidence reduction — not a deletion.

Automation ceiling: low-risk confidence adjustments may apply automatically.
Deletion, cross-user sharing and anything touching sensitive material require a
human. Scope never widens beyond user / workspace / team.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

OP_WRITE = "write"
OP_UPDATE = "update"
OP_MERGE = "merge"
OP_DELETE = "delete"
OP_REWEIGHT = "reweight"
OP_READ_POLICY = "read_policy"

# Operations safe to apply without review. Everything else escalates — the
# asymmetry is deliberate: a wrong reweight is recoverable, a wrong deletion or
# a cross-user leak is not.
AUTO_APPLICABLE = frozenset({OP_REWEIGHT})

SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
SCOPE_TEAM = "team"
ALLOWED_SCOPES = (SCOPE_USER, SCOPE_WORKSPACE, SCOPE_TEAM)

SUPPRESSED_BY_CURRENT_REQUEST = "suppressed_by_current_request"


@dataclass
class MemoryOp:
    """One proposed change to memory content or retrieval policy."""

    operation: str
    memory_ref: str = ""
    scope: str = SCOPE_USER
    reason: str = ""
    before: Any = None
    after: Any = None
    sensitive: bool = False
    cross_user: bool = False

    @property
    def requires_human(self) -> bool:
        """Whether this op must be reviewed before it can take effect."""
        if self.operation not in AUTO_APPLICABLE:
            return True
        return bool(self.sensitive or self.cross_user)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "memory_ref": self.memory_ref,
            "scope": self.scope,
            "reason": self.reason,
            "before": self.before,
            "after": self.after,
            "requires_human": self.requires_human,
        }


@dataclass
class RetrievalPolicyChange:
    """A change to when and how memories are injected."""

    rule: str
    description: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "description": self.description, "payload": self.payload}


def validate_scope(scope: str, allowed: Sequence[str] = ALLOWED_SCOPES) -> Tuple[bool, str]:
    if scope not in allowed:
        return False, f"scope {scope} outside {list(allowed)}"
    return True, ""


def diagnose_preference_conflict(
    *,
    injected_preference: str,
    current_instruction: str,
    outcome_failed: bool,
    prompt_declares_priority: bool,
) -> Dict[str, Any]:
    """Decide whether the content or the injection policy is at fault.

    Returns the diagnosis plus the operations it implies. The distinction
    matters because deleting a still-valid long-term preference to fix a
    one-turn override trades a small problem for a larger one.
    """
    conflict = bool(injected_preference and current_instruction)
    if not conflict or not outcome_failed:
        return {"verdict": "no_conflict", "ops": [], "policy_changes": []}

    ops: List[MemoryOp] = []
    policy_changes: List[RetrievalPolicyChange] = []

    if not prompt_declares_priority:
        # The retriever did its job; nothing downstream was told that an
        # explicit current instruction outranks a standing soft preference.
        policy_changes.append(
            RetrievalPolicyChange(
                rule=SUPPRESSED_BY_CURRENT_REQUEST,
                description=(
                    "交付格式的当前显式要求覆盖历史软偏好；偏好仍可检索，"
                    "但标记为被本轮约束压制，不进入可执行建议区"
                ),
                payload={"applies_to": "delivery_format"},
            )
        )
        verdict = "retrieval_policy"
    else:
        verdict = "memory_content"

    # Either way the preference is now less certain — but it is down-weighted,
    # never deleted, so a later turn can still surface it.
    ops.append(
        MemoryOp(
            operation=OP_REWEIGHT,
            reason="与本轮显式指令冲突，降低置信度但保留",
            before=0.88,
            after=0.55,
        )
    )

    return {
        "verdict": verdict,
        "ops": [op.to_dict() for op in ops],
        "policy_changes": [c.to_dict() for c in policy_changes],
    }


def build_replay_plan(policy_changes: Sequence[RetrievalPolicyChange]) -> Dict[str, Any]:
    """The replay set a memory candidate must clear.

    Deliberately two-sided: it is not enough to stop honouring the stale
    preference, the system must also still honour it when the user has *not*
    overridden it. A one-sided replay would happily approve a change that simply
    forgot the preference altogether.
    """
    return {
        "cohorts": [
            {
                "name": "honours_standing_preference",
                "description": "用户未提出覆盖时，仍应沿用长期偏好",
                "must_pass": True,
            },
            {
                "name": "respects_explicit_override",
                "description": "用户显式要求时，必须覆盖长期偏好",
                "must_pass": True,
            },
        ],
        "policy_changes": [c.to_dict() for c in policy_changes],
    }


def partition_by_approval(ops: Sequence[MemoryOp]) -> Tuple[List[MemoryOp], List[MemoryOp]]:
    """Split into (auto-applicable, needs-human)."""
    auto = [op for op in ops if not op.requires_human]
    manual = [op for op in ops if op.requires_human]
    return auto, manual
