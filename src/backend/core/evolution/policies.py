"""The autonomous loop's view of the orchestration profile.

This used to be a separate asset with its own store, its own release rows and
its own resolution path. That arrangement had three defects and only the third
was ever visible:

* **It was the wrong target.** A "workflow policy" only ever reached
  ``autonomous_loop`` — the long-running self-driving product — while the main
  ReAct axis, which handles nearly all traffic, read none of it. Calling that
  layer "orchestration evolution" overstated what it governed by roughly the
  ratio of the two traffic volumes.
* **It resolved at import.** The loop read it once into module constants, so a
  published policy needed a process restart to take effect and separate replicas
  disagreed until they were all restarted.
* **It ignored the tenant.** The lookup was by mode alone, so one tenant
  publishing a policy changed retry counts and budgets for every tenant. The
  fix for that shipped as a ``tenant_id`` parameter no caller passed.

All three are gone. There is one orchestration asset —
:class:`~core.evolution.agent_profile.AgentProfile` — and this module is the
**projection** of it that the autonomous loop consumes. A projection rather than
a copy: there is no second store to publish to, no second thing to keep in sync,
and no way for the loop's numbers to disagree with the profile they came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.evolution.agent_profile import (
    ACTION_CHANGE_STRATEGY,
    ACTION_ESCALATE,
    ACTION_NARROW_SCOPE,
    ACTION_RETRY,
    ACTION_ROLLBACK_AND_FORK,
    ACTION_STOP,
    ALLOWED_ACTIONS,
    SIGNAL_BUDGET_LOW,
    SIGNAL_NO_DIFF,
    SIGNAL_NO_PROGRESS,
    SIGNAL_REPEATED_ACTIONS,
    SIGNAL_REVIEWER_FLAT,
    AgentProfile,
    InterventionRule,
    load_active_profile,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_CHANGE_STRATEGY",
    "ACTION_ESCALATE",
    "ACTION_NARROW_SCOPE",
    "ACTION_RETRY",
    "ACTION_ROLLBACK_AND_FORK",
    "ACTION_STOP",
    "ALLOWED_ACTIONS",
    "SIGNAL_BUDGET_LOW",
    "SIGNAL_NO_DIFF",
    "SIGNAL_NO_PROGRESS",
    "SIGNAL_REPEATED_ACTIONS",
    "SIGNAL_REVIEWER_FLAT",
    "InterventionRule",
    "LoopPolicy",
    "load_loop_policy",
    "policy_from_profile",
]

@dataclass
class LoopPolicy:
    """What ``autonomous_loop`` needs from the active profile."""

    version: str = "builtin"
    max_attempts_per_requirement: int = 6
    strategy_change_after: int = 2
    rules: List[InterventionRule] = field(default_factory=list)
    reviewer_level: str = "checkpoint"
    budget_multiplier: float = 1.0

    def action_for(self, signal_counts: Dict[str, int]) -> Optional[str]:
        """Pick the intervention for the current signals.

        Rules are evaluated most-specific-first (highest threshold wins) so a
        long stall escalates rather than repeating the mild response that
        already failed to help.
        """
        applicable = [
            rule
            for rule in self.rules
            if signal_counts.get(rule.signal, 0) >= rule.threshold
        ]
        if not applicable:
            return None
        return max(applicable, key=lambda r: r.threshold).action

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "max_attempts_per_requirement": self.max_attempts_per_requirement,
            "strategy_change_after": self.strategy_change_after,
            "reviewer_level": self.reviewer_level,
            "budget_multiplier": self.budget_multiplier,
            "rules": [r.to_dict() for r in self.rules],
        }


def policy_from_profile(profile: AgentProfile) -> LoopPolicy:
    """Project a profile onto the loop's knobs."""
    return LoopPolicy(
        version=profile.version,
        max_attempts_per_requirement=profile.loop_max_attempts_per_requirement,
        strategy_change_after=profile.loop_strategy_change_after,
        rules=list(profile.intervention_rules),
        reviewer_level=(profile.reviewer_checkpoints or ["checkpoint"])[0],
        budget_multiplier=profile.budget_multiplier,
    )


def load_loop_policy(*, tenant_id: str, user_id: str = "") -> LoopPolicy:
    """The loop's policy for **this run**.

    Resolved per run and per tenant. Both are load-bearing: resolving once at
    import made publishing require a restart, and resolving without a tenant
    made one tenant's tuning everyone's.
    """
    profile = load_active_profile(
        task_type="autonomous_loop", user_id=user_id, tenant_id=tenant_id
    )
    return policy_from_profile(profile)
