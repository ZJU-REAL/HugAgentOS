"""Evolution IR — one shell for every kind of change (GCE ticket 13).

The IR does not force memory, skills, workflows and ontology into one data
shape; their change semantics genuinely differ.  What it unifies is the
*envelope*: what is being changed, from which base version, why, on what
evidence, at what risk, and how to undo it.  That envelope is what lets four
very different assets share one evaluation and release pipeline.

The other half of this module is the first gate a candidate passes: invariant
validation.  Its job is security, not quality.  Self-evolution must never become
a new privilege-escalation path, so a candidate that tries to widen tool
authority, cross a scope boundary, rewrite source code, or bypass human approval
is rejected here — before a human ever sees it, and regardless of how good the
evidence looks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"

# ── Operations, per asset kind ───────────────────────────────────────────────
# Anything not on a list is refused. An allow-list rather than a deny-list
# because the dangerous operations are the ones nobody thought to forbid.

OP_WHITELIST: Dict[str, Tuple[str, ...]] = {
    "memory": ("write", "update", "merge", "delete", "reweight", "read_policy"),
    "skill": ("new", "patch", "merge", "deprecate", "test"),
    # Note the absence of anything resembling "edit_source": orchestration
    # evolves declarative policy only. Arbitrary self-modifying code is the one
    # capability this design refuses outright.
    #
    # ``agent_profile`` is what orchestration evolution actually targets — the
    # assembly a ReAct turn is built from (prompt fragments, tool allowlist,
    # skill set, subagent routes, budgets, intervention rules). ``workflow``
    # remains for the autonomous loop's own knobs, which are a projection of the
    # same profile.
    "agent_profile": ("new", "patch", "activate", "deprecate"),
    "subagent": ("new", "patch", "deprecate"),
    "workflow": ("route", "dag", "budget", "reviewer", "rollback"),
    "ontology": ("term", "relation", "constraint", "workflow", "false_positive"),
    "prompt": ("patch", "new", "activate"),
}

# Kinds whose changes alter what capability the agent has, rather than what it
# knows. They are held to the tool-authority check even when the proposer did
# not declare ``requested_tools``, because the tools are inside the payload.
CAPABILITY_KINDS = ("agent_profile", "subagent")

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_TIERS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

# Minimum verification each risk tier must clear before activation.
MIN_VERIFICATION = {
    RISK_LOW: ("replay",),
    RISK_MEDIUM: ("replay", "shadow"),
    RISK_HIGH: ("replay", "shadow", "canary", "human_approval"),
}


def _tools_in_changes(changes: Sequence[Dict[str, Any]]) -> List[str]:
    """Every tool a capability change would grant.

    Both keys are read because a profile declares ``tool_allowlist`` and a
    sub-agent declares ``tools_allowlist``; a gate that knew only one of them
    would be exactly as good as no gate for the other.
    """
    tools: List[str] = []
    for change in changes or []:
        for key in ("tool_allowlist", "tools_allowlist", "requested_tools"):
            value = change.get(key)
            if isinstance(value, (list, tuple)):
                tools.extend(str(item) for item in value)
    return tools


class InvariantViolation(Exception):
    """A candidate that must never reach a reviewer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Rollback:
    version_id: str = ""
    triggers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"version_id": self.version_id, "triggers": list(self.triggers)}


@dataclass
class EvolutionIR:
    """The change envelope shared by every asset kind."""

    target_kind: str
    target_asset_id: str
    base_version: str
    operation: str
    hypothesis: str = ""
    changes: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    evaluation_plan: Dict[str, Any] = field(default_factory=dict)
    rollback: Rollback = field(default_factory=Rollback)
    risk_tier: str = RISK_MEDIUM
    scope: Dict[str, str] = field(default_factory=dict)
    # Tool grants this change would require. Compared against the base version's
    # grants during validation; widening is refused.
    requested_tools: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target": {
                "kind": self.target_kind,
                "asset_id": self.target_asset_id,
                "base_version": self.base_version,
            },
            "operation": self.operation,
            "hypothesis": self.hypothesis,
            "changes": self.changes,
            "evidence_refs": list(self.evidence_refs),
            "constraints": list(self.constraints),
            "evaluation_plan": self.evaluation_plan,
            "rollback": self.rollback.to_dict(),
            "risk_tier": self.risk_tier,
            "scope": self.scope,
            "requested_tools": list(self.requested_tools),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EvolutionIR":
        target = raw.get("target") or {}
        rollback = raw.get("rollback") or {}
        return cls(
            target_kind=str(target.get("kind") or ""),
            target_asset_id=str(target.get("asset_id") or ""),
            base_version=str(target.get("base_version") or ""),
            operation=str(raw.get("operation") or ""),
            hypothesis=str(raw.get("hypothesis") or ""),
            changes=raw.get("changes") or [],
            evidence_refs=raw.get("evidence_refs") or [],
            constraints=raw.get("constraints") or [],
            evaluation_plan=raw.get("evaluation_plan") or {},
            rollback=Rollback(
                version_id=str(rollback.get("version_id") or ""),
                triggers=rollback.get("triggers") or [],
            ),
            risk_tier=str(raw.get("risk_tier") or RISK_MEDIUM),
            scope=raw.get("scope") or {},
            requested_tools=raw.get("requested_tools") or [],
            schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
            created_at=str(raw.get("created_at") or ""),
        )

    def change_checksum(self) -> str:
        """Content hash of the change, for de-duplication.

        Excludes timestamps and evidence refs deliberately: the same edit
        proposed twice from different evidence is still the same edit, and
        repeated evidence must not flood the review queue.
        """
        payload = json.dumps(
            {
                "kind": self.target_kind,
                "asset": self.target_asset_id,
                "base": self.base_version,
                "op": self.operation,
                "changes": self.changes,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ── Invariant validation ─────────────────────────────────────────────────────


def validate_ir(
    ir: EvolutionIR,
    *,
    base_tools: Optional[Sequence[str]] = None,
    allowed_scopes: Optional[Sequence[str]] = None,
    credit_decision: Optional[Dict[str, Any]] = None,
    known_concepts: Optional[Sequence[str]] = None,
) -> None:
    """Reject anything that must never reach a human reviewer.

    Raises :class:`InvariantViolation`. Everything here is a security property;
    quality judgements belong later in the pipeline.
    """
    # Checked before the generic whitelist so the rejection explains the reason.
    # "activate" is absent from the ontology whitelist too, but an author who
    # tries it deserves to be told the governance rule, not just "not allowed".
    if ir.target_kind == "ontology" and ir.operation == "activate":
        raise InvariantViolation(
            "ontology_self_activation", "本体候选永不允许自动激活"
        )

    # Structural
    if ir.target_kind not in OP_WHITELIST:
        raise InvariantViolation("unknown_kind", f"未知资产类型 {ir.target_kind}")
    if ir.operation not in OP_WHITELIST[ir.target_kind]:
        raise InvariantViolation(
            "operation_not_allowed",
            f"{ir.target_kind} 不允许操作 {ir.operation}",
        )
    if not ir.target_asset_id:
        raise InvariantViolation("missing_target", "候选必须指明目标资产")
    if ir.risk_tier not in RISK_TIERS:
        raise InvariantViolation("bad_risk_tier", f"未知风险等级 {ir.risk_tier}")

    # Every candidate must be traceable to an attribution decision. Without it a
    # change has no accountable reason to exist.
    if credit_decision is None:
        raise InvariantViolation("missing_credit", "候选必须引用一个归因决策")
    if not credit_decision.get("may_produce_candidate", False):
        raise InvariantViolation(
            "credit_forbids_candidate",
            "该归因决策不允许产出资产候选（低置信 / 不可识别 / 非资产原因）",
        )

    # Privilege escalation: the whole point of the gate.
    if base_tools is not None:
        widened = set(ir.requested_tools) - set(base_tools)
        # For capability kinds the tools live in the change payload, not in
        # ``requested_tools``. Reading only the declared field would let a
        # profile or sub-agent grant itself a tool simply by not mentioning it
        # in the field the gate happens to inspect.
        if ir.target_kind in CAPABILITY_KINDS:
            widened |= set(_tools_in_changes(ir.changes)) - set(base_tools)
        if widened:
            raise InvariantViolation(
                "tool_escalation",
                f"候选试图扩大工具授权: {sorted(widened)}",
            )

    # A sub-agent that can spawn sub-agents turns one bad routing decision into
    # an unbounded one, and the budget and context costs compound at every
    # level. One layer, enforced structurally.
    if ir.target_kind == "subagent":
        for change in ir.changes:
            if change.get("may_delegate"):
                raise InvariantViolation(
                    "subagent_nesting", "子智能体不得再派生子智能体（限一层）"
                )

    # Scope: a candidate learned in one tenant must not silently generalise.
    if allowed_scopes is not None and ir.scope:
        scope_value = ir.scope.get("level") or ""
        if scope_value and scope_value not in allowed_scopes:
            raise InvariantViolation(
                "scope_escalation",
                f"候选作用域 {scope_value} 超出允许范围 {list(allowed_scopes)}",
            )

    # Rollback is not optional. A change nobody knows how to undo must not ship.
    if ir.operation not in ("test",) and not ir.rollback.version_id:
        raise InvariantViolation("missing_rollback", "候选必须声明回滚目标版本")

    # Dangling references break the pack for everyone, not just this candidate.
    if known_concepts is not None and ir.target_kind == "ontology":
        for change in ir.changes:
            ref = str(change.get("concept") or "")
            if ref and ref not in known_concepts:
                raise InvariantViolation(
                    "dangling_concept", f"候选引用了不存在的概念 {ref}"
                )


def required_verification(risk_tier: str) -> Tuple[str, ...]:
    return MIN_VERIFICATION.get(risk_tier, MIN_VERIFICATION[RISK_HIGH])


def new_candidate_id() -> str:
    return f"cand_{uuid.uuid4().hex[:20]}"
