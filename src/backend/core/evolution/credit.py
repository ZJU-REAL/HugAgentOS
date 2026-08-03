"""Credit assignment — decide *whether* to update, then *what* (GCE ticket 11).

This is what separates the design from "three engines each learning on their
own".  When several components can all change themselves, a single failure sends
every one of them a signal, and they produce mutually contradictory edits.  The
assigner's job is to pick at most one responsible target — or to decide that
nothing should change at all.

**The attribution space is deliberately larger than the four mutable assets.**
This deployment runs multiple model providers, multiple knowledge bases, a
sandbox and a pile of external tools.  In practice a large share of real
failures are "the KB simply has no such document", "this model cannot produce
that structured output", or "an external API changed its schema yesterday".  If
the only choices were memory / skill / workflow / ontology, the assigner would
be *forced* to squeeze those into a mutable asset — labelling a knowledge gap as
"this skill needs patching", emitting a candidate that can never fix anything,
and polluting the statistics on the way. That failure mode has a name here:
**pseudo-evolution**, and excluding it is a design requirement, not a nicety.

The first version is explainable rules plus calibrated confidence — no trained
model. Low confidence, unexplainable signals and suspected environment faults
all resolve to NO_UPDATE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Attribution targets ──────────────────────────────────────────────────────

T_MEMORY = "memory"
T_SKILL = "skill"
T_ORCHESTRATION = "orchestration"
T_ONTOLOGY = "ontology"
T_PROMPT = "prompt"
T_RETRIEVAL = "retrieval"      # KB gap → a "add the document" ticket, not a patch
T_MODEL = "model"              # capability mismatch → routing/config, not a patch
T_ENVIRONMENT = "environment"  # diagnose and alert only; never emits a candidate
T_NO_UPDATE = "no_update"

ALL_TARGETS: Tuple[str, ...] = (
    T_MEMORY,
    T_SKILL,
    T_ORCHESTRATION,
    T_ONTOLOGY,
    T_PROMPT,
    T_RETRIEVAL,
    T_MODEL,
    T_ENVIRONMENT,
    T_NO_UPDATE,
)

# Targets that may never produce a mutable-asset candidate. Keeping this
# explicit is what stops a KB gap from being "fixed" by editing a skill.
NON_ASSET_TARGETS = frozenset({T_RETRIEVAL, T_MODEL, T_ENVIRONMENT, T_NO_UPDATE})

# Below this, we refuse to name a target. Being unsure is a legitimate answer;
# guessing produces permanent edits from noise.
MIN_CONFIDENCE = 0.45
# When the top two targets are this close, the evidence cannot separate them.
UNIDENTIFIABLE_MARGIN = 0.08

VERDICT_UNIDENTIFIABLE = "unidentifiable"
# A finding that came from aggregate observation across many runs rather than
# from attributing one failure. Kept distinct because the two answer different
# questions and only one of them is about blame.
VERDICT_AGGREGATE = "aggregate_observation"


@dataclass
class CreditFeatures:
    """Observable signals the rules read. All optional; absence lowers confidence."""

    outcome_verdict: str = "unknown"
    outcome_confidence: float = 0.0
    # Memory
    memory_injected: int = 0
    memory_conflicts_current_request: bool = False
    memory_degraded_reason: str = ""
    # Skill
    repeated_tool_subsequence: bool = False
    skill_selected_count: int = 0
    skill_rejected_count: int = 0
    # Workflow
    loop_stagnation: bool = False
    no_diff_iterations: int = 0
    step_order_suspect: bool = False
    # Ontology
    gate_denied_count: int = 0
    gate_false_positive_evidence: bool = False
    # Non-asset causes
    kb_empty_result: bool = False
    tool_5xx_count: int = 0
    tool_schema_error: bool = False
    model_capability_miss: bool = False
    # Cross-cutting
    prompt_missing_priority_rule: bool = False

    def evidence_count(self) -> int:
        """How many signals are actually present, for confidence calibration."""
        return sum(
            1
            for value in (
                self.memory_injected,
                self.memory_conflicts_current_request,
                self.repeated_tool_subsequence,
                self.loop_stagnation,
                self.no_diff_iterations,
                self.step_order_suspect,
                self.gate_denied_count,
                self.gate_false_positive_evidence,
                self.kb_empty_result,
                self.tool_5xx_count,
                self.tool_schema_error,
                self.model_capability_miss,
                self.skill_selected_count,
                self.skill_rejected_count,
                self.prompt_missing_priority_rule,
            )
            if value
        )


@dataclass
class CreditDecision:
    """One attribution verdict, fully reconstructible after the fact."""

    selected: str = T_NO_UPDATE
    scores: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    explanation: str = ""
    verdict: str = ""
    # Multi-label: several targets may share responsibility.
    also_implicated: List[str] = field(default_factory=list)
    features: Dict[str, Any] = field(default_factory=dict)
    assigner_version: str = "rules-1"

    @property
    def may_produce_candidate(self) -> bool:
        """Whether this verdict is allowed to generate an asset change."""
        return (
            self.selected not in NON_ASSET_TARGETS
            and self.verdict != VERDICT_UNIDENTIFIABLE
            and self.confidence >= MIN_CONFIDENCE
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected": self.selected,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "confidence": round(self.confidence, 3),
            "explanation": self.explanation,
            "verdict": self.verdict,
            "also_implicated": list(self.also_implicated),
            "features": self.features,
            "assigner_version": self.assigner_version,
            "may_produce_candidate": self.may_produce_candidate,
        }


def _score_targets(f: CreditFeatures) -> Dict[str, float]:
    """Explainable rules. Every increment below is a sentence a human can check."""
    scores: Dict[str, float] = {t: 0.0 for t in ALL_TARGETS}

    # ── Non-asset causes are scored FIRST and weighted heavily. ──
    # If the KB has no document, no amount of skill editing will help; letting a
    # mutable asset outrank this is exactly how pseudo-evolution happens.
    if f.kb_empty_result:
        scores[T_RETRIEVAL] += 0.75
    if f.tool_schema_error:
        scores[T_ENVIRONMENT] += 0.80
    if f.tool_5xx_count >= 2:
        scores[T_ENVIRONMENT] += 0.65
    elif f.tool_5xx_count == 1:
        scores[T_ENVIRONMENT] += 0.30
    if f.model_capability_miss:
        scores[T_MODEL] += 0.70

    # ── Memory ──
    if f.memory_conflicts_current_request:
        scores[T_MEMORY] += 0.70
        # A conflict may equally mean the prompt never said "current instruction
        # wins" — that confound is explicit rather than silently ignored.
        scores[T_PROMPT] += 0.25
    if f.memory_injected and f.outcome_verdict == "failed":
        scores[T_MEMORY] += 0.15

    # ── Skill ──
    if f.repeated_tool_subsequence:
        scores[T_SKILL] += 0.65
        # A stable subsequence might be a fixed workflow rather than a reusable
        # skill; keep the alternative on the board.
        scores[T_ORCHESTRATION] += 0.20
    if f.skill_rejected_count and not f.skill_selected_count and f.outcome_verdict == "failed":
        scores[T_SKILL] += 0.20

    # ── Workflow ──
    if f.loop_stagnation:
        scores[T_ORCHESTRATION] += 0.60
    if f.no_diff_iterations >= 2:
        scores[T_ORCHESTRATION] += 0.25
    if f.step_order_suspect:
        scores[T_ORCHESTRATION] += 0.45

    # ── Ontology ──
    if f.gate_false_positive_evidence and f.gate_denied_count >= 2:
        scores[T_ONTOLOGY] += 0.70
    elif f.gate_denied_count >= 3:
        scores[T_ONTOLOGY] += 0.35

    # ── Prompt ──
    if f.prompt_missing_priority_rule:
        scores[T_PROMPT] += 0.55

    # Nothing went wrong → no update is the correct answer, not a fallback.
    if f.outcome_verdict == "success":
        scores[T_NO_UPDATE] += 0.80
    elif f.outcome_verdict == "unknown":
        scores[T_NO_UPDATE] += 0.45

    return scores


def _explain(selected: str, f: CreditFeatures) -> str:
    reasons = {
        T_MEMORY: "当前请求与注入记忆存在显式冲突",
        T_SKILL: "出现稳定重复的工具子序列，但每次都在重新规划",
        # Names the *assembly* — tools, skills, prompt fragments, routes — not a
        # DAG. The product's main axis is a ReAct loop, which executes no DAG,
        # so "workflow" pointed reviewers at a thing that does not exist here.
        T_ORCHESTRATION: "过程停滞：重复动作、无产出增量",
        T_ONTOLOGY: "同一规则重复拒绝且有误报证据",
        T_PROMPT: "提示词未声明当前指令优先",
        T_RETRIEVAL: "知识库检索为空——补文档，不是改技能",
        T_MODEL: "失败集中在该模型能力边界",
        T_ENVIRONMENT: "外部工具/接口故障，属环境问题，不生成候选",
        T_NO_UPDATE: "无充分证据支持任何持久修改",
    }
    return reasons.get(selected, "")


def aggregate_finding(
    *, target: str, explanation: str, confidence: float, evidence_count: int
) -> CreditDecision:
    """A decision for something observed across many runs, not blamed on one.

    :func:`assign_credit` answers "this turn failed — whose fault?". Several
    engines do not ask that question at all: "this task type never uses eight of
    its twelve tools" and "these two memories say the same thing" are
    observations over a corpus, most of it *successful*.

    Routing those through the failure attributor required inventing failure
    features to make it return the desired target — and the console then
    displayed the canned explanation for those invented features, so an
    orchestration candidate whose real reason was "eight tools were never used"
    was shown to reviewers as "过程停滞：重复动作、无产出增量". That is a claim
    about evidence nobody measured, printed in the one column a reviewer reads
    first.

    So aggregate findings get their own decision, carrying the reason they
    actually have. Confidence is derived from how much evidence stands behind
    the observation rather than asserted, and the same governance gate applies:
    below :data:`MIN_CONFIDENCE` it may not produce a candidate.
    """
    return CreditDecision(
        selected=target,
        scores={target: round(confidence, 3)},
        confidence=round(confidence, 3),
        explanation=explanation,
        verdict=VERDICT_AGGREGATE,
        features={"evidence_count": int(evidence_count), "kind": "aggregate"},
        assigner_version="aggregate-1",
    )


def confidence_from_evidence(count: int, *, saturates_at: int = 30) -> float:
    """More corroboration means more confidence, with a ceiling.

    Linear up to ``saturates_at`` and capped below 1.0: no amount of aggregate
    observation makes a proposal certain, because the corpus only shows what was
    tried, never what would have worked instead.
    """
    if count <= 0:
        return 0.0
    return min(0.9, 0.35 + 0.55 * min(1.0, count / max(1, saturates_at)))


def assign_credit(features: CreditFeatures) -> CreditDecision:
    """Pick the responsible target, or decline to.

    Deterministic: the same features always produce the same decision. That is a
    requirement, not an implementation detail — an attribution you cannot
    reproduce cannot be audited or ablated.
    """
    scores = _score_targets(features)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    second, second_score = ranked[1] if len(ranked) > 1 else (T_NO_UPDATE, 0.0)

    # Confidence is driven mainly by signal strength; breadth is a modifier, not
    # a halving. One decisive signal (an external schema change, say) is genuinely
    # conclusive on its own, and treating it as weak would push obvious
    # environment faults into No-Update — losing the very diagnosis that stops
    # them being mistaken for asset problems.
    evidence = features.evidence_count()
    breadth = min(1.0, evidence / 3.0)
    outcome_factor = max(0.75, features.outcome_confidence or 0.75)
    confidence = round(min(1.0, top_score) * (0.7 + 0.3 * breadth) * outcome_factor, 4)

    # Low bar on purpose: this list is "what else the evidence touches", used to
    # keep confounds visible, not to name a second culprit.
    also = [name for name, value in ranked[1:4] if value >= 0.15]

    # Interacting assets make single-variable attribution biased. Say so rather
    # than forcing a winner the evidence does not support.
    if top_score > 0 and (top_score - second_score) < UNIDENTIFIABLE_MARGIN and top != T_NO_UPDATE:
        return CreditDecision(
            selected=T_NO_UPDATE,
            scores=scores,
            confidence=confidence,
            verdict=VERDICT_UNIDENTIFIABLE,
            explanation=f"证据无法区分 {top} 与 {second}，不做归因",
            also_implicated=[top, second],
            features=features.__dict__.copy(),
        )

    if top_score <= 0 or confidence < MIN_CONFIDENCE:
        return CreditDecision(
            selected=T_NO_UPDATE,
            scores=scores,
            confidence=confidence,
            explanation="置信度不足，落 No-Update",
            also_implicated=also,
            features=features.__dict__.copy(),
        )

    return CreditDecision(
        selected=top,
        scores=scores,
        confidence=confidence,
        explanation=_explain(top, features),
        also_implicated=also,
        features=features.__dict__.copy(),
    )
