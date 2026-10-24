"""Replay engine — single-variable counterfactual evaluation (GCE ticket 16).

Answers "is this candidate better than the base?" by running the same tasks
twice, swapping exactly one asset version and freezing everything else.

Three commitments that shape the implementation:

**Replay screens; canary decides.**  The replay set is sampled from a past
policy and its tool environment has since drifted, so ``do(Δz)`` here is
interventional only with respect to the replay distribution.  Replay is
therefore a high-recall, low-precision filter whose job is to eliminate
obviously harmful candidates; the headline effect size belongs to the canary,
which is the one genuinely randomised comparison in the whole design.

**Paired statistics, not two independent samples.**  Both arms run the *same*
tasks.  At realistic set sizes an unpaired proportion test cannot see a few
percentage points, so an unpaired design would make the whole evaluation
theatre.  McNemar's test on the discordant pairs is what makes the difference
detectable.

**Cost is a first-class constraint.**  Replay set × 2 arms × candidates × tokens
grows fast enough to exceed the product's own inference bill.  Hence sentinel
pre-screening: a small subset runs first and only survivors earn a full run.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERDICT_IMPROVED = "improved"
VERDICT_NEUTRAL = "neutral"
VERDICT_REGRESSED = "regressed"
VERDICT_INSUFFICIENT = "insufficient_evidence"

# A candidate must clear this to advance. Deliberately paired with a
# significance requirement: a large effect on six tasks is noise.
MIN_EFFECT_PP = 0.05
MAX_P_VALUE = 0.05
MIN_SAMPLE = 20

# Sentinel pre-screen size. Small enough to be nearly free, large enough to
# catch a candidate that is outright broken.
SENTINEL_SIZE = 8


@dataclass
class TaskResult:
    task_id: str
    success: bool
    cost_usd: float = 0.0
    latency_ms: int = 0
    risk_denied: bool = False


@dataclass
class ArmMetrics:
    successes: int = 0
    total: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    risk_denials: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "successes": self.successes,
            "total": self.total,
            "success_rate": round(self.success_rate, 4),
            "cost_usd": round(self.cost_usd, 4),
            "avg_latency_ms": int(self.latency_ms / self.total) if self.total else 0,
            "risk_denials": self.risk_denials,
        }


@dataclass
class ReplayReport:
    evaluation_id: str = field(default_factory=lambda: f"eval_{uuid.uuid4().hex[:20]}")
    baseline: ArmMetrics = field(default_factory=ArmMetrics)
    candidate: ArmMetrics = field(default_factory=ArmMetrics)
    verdict: str = VERDICT_INSUFFICIENT
    effect_size: float = 0.0
    p_value: float = 1.0
    sample_size: int = 0
    # Tasks where the two arms disagreed — the only ones a paired test uses.
    discordant_pairs: Tuple[int, int] = (0, 0)
    dataset_snapshot: List[str] = field(default_factory=list)
    guardrail_breaches: List[str] = field(default_factory=list)
    cassette_report: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "verdict": self.verdict,
            "effect_size": round(self.effect_size, 4),
            "p_value": round(self.p_value, 5),
            "sample_size": self.sample_size,
            "discordant_pairs": list(self.discordant_pairs),
            "dataset_snapshot": self.dataset_snapshot,
            "guardrail_breaches": self.guardrail_breaches,
            "cassette": self.cassette_report,
            "notes": self.notes,
        }


def mcnemar_p_value(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for small discordant counts.

    Only the pairs where the arms disagreed carry information; tasks both arms
    got right (or both wrong) tell us nothing about the difference and are
    correctly excluded. This is precisely why pairing buys so much power.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # Binomial tail under H0: each discordant pair is a fair coin.
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def compare_paired(
    baseline: Sequence[TaskResult], candidate: Sequence[TaskResult]
) -> ReplayReport:
    """Compare two arms that ran the identical task set."""
    report = ReplayReport()

    by_base = {r.task_id: r for r in baseline}
    by_cand = {r.task_id: r for r in candidate}
    shared = sorted(set(by_base) & set(by_cand))
    report.dataset_snapshot = shared
    report.sample_size = len(shared)

    if not shared:
        report.notes.append("no overlapping tasks between arms")
        return report

    # b: baseline right, candidate wrong. c: the reverse.
    b = c = 0
    for task_id in shared:
        base_result, cand_result = by_base[task_id], by_cand[task_id]

        report.baseline.total += 1
        report.candidate.total += 1
        report.baseline.successes += int(base_result.success)
        report.candidate.successes += int(cand_result.success)
        report.baseline.cost_usd += base_result.cost_usd
        report.candidate.cost_usd += cand_result.cost_usd
        report.baseline.latency_ms += base_result.latency_ms
        report.candidate.latency_ms += cand_result.latency_ms
        report.baseline.risk_denials += int(base_result.risk_denied)
        report.candidate.risk_denials += int(cand_result.risk_denied)

        if base_result.success and not cand_result.success:
            b += 1
        elif cand_result.success and not base_result.success:
            c += 1

    report.discordant_pairs = (b, c)
    report.effect_size = report.candidate.success_rate - report.baseline.success_rate
    report.p_value = mcnemar_p_value(b, c)

    # A safety regression is disqualifying regardless of the quality delta —
    # a candidate that answers better while tripping the gate more often has
    # not improved, it has learned to take more risk.
    if report.candidate.risk_denials > report.baseline.risk_denials:
        report.guardrail_breaches.append("risk_denials_increased")
        report.verdict = VERDICT_REGRESSED
        return report

    # The sample-size floor is deliberately asymmetric: it guards against
    # *claiming an improvement* on thin evidence, but it must not suppress
    # *detecting a regression*. Missing a regression is far more costly than
    # withholding a "not yet proven better", and a sentinel pre-screen whose
    # whole job is to catch broken candidates would be useless otherwise.
    if (
        report.effect_size <= -MIN_EFFECT_PP
        and report.p_value <= MAX_P_VALUE
    ):
        report.verdict = VERDICT_REGRESSED
        if report.sample_size < MIN_SAMPLE:
            report.notes.append(
                f"regression significant on only {report.sample_size} tasks; "
                "reported anyway because suppressing it would defeat the pre-screen"
            )
        return report

    if report.sample_size < MIN_SAMPLE:
        report.verdict = VERDICT_INSUFFICIENT
        report.notes.append(
            f"sample {report.sample_size} < required {MIN_SAMPLE}"
        )
        return report

    if report.effect_size >= MIN_EFFECT_PP and report.p_value <= MAX_P_VALUE:
        report.verdict = VERDICT_IMPROVED
    elif report.effect_size <= -MIN_EFFECT_PP and report.p_value <= MAX_P_VALUE:
        report.verdict = VERDICT_REGRESSED
    else:
        report.verdict = VERDICT_NEUTRAL
    return report


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.05) -> List[bool]:
    """Which hypotheses survive at FDR ``alpha``.

    Candidates are evaluated in batches and re-evaluated over time; without a
    correction the pass rate drifts upward purely through repetition, and the
    system would look like it is learning when it is only testing more often.
    """
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda kv: kv[1])
    survives = [False] * n
    threshold_rank = -1
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= alpha * rank / n:
            threshold_rank = rank
    for rank, (index, _) in enumerate(indexed, start=1):
        if rank <= threshold_rank:
            survives[index] = True
    return survives


@dataclass
class TieredResult:
    passed_sentinel: bool
    report: Optional[ReplayReport]
    sentinel_report: Optional[ReplayReport]
    tasks_run: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed_sentinel": self.passed_sentinel,
            "tasks_run": self.tasks_run,
            "sentinel": self.sentinel_report.to_dict() if self.sentinel_report else None,
            "full": self.report.to_dict() if self.report else None,
        }


def run_tiered_replay(
    task_ids: Sequence[str],
    run_arm: Callable[[str, bool], TaskResult],
    *,
    sentinel_size: int = SENTINEL_SIZE,
) -> TieredResult:
    """Sentinel pre-screen, then full replay only for survivors.

    ``run_arm(task_id, is_candidate)`` executes one task under one arm. Injected
    so the statistics can be tested without an agent, and so the caller controls
    isolation (sandbox snapshot, cassette, frozen bundle).
    """
    tasks = list(task_ids)
    if not tasks:
        return TieredResult(False, None, None, 0)

    sentinel = tasks[: max(1, min(sentinel_size, len(tasks)))]
    base_s = [run_arm(t, False) for t in sentinel]
    cand_s = [run_arm(t, True) for t in sentinel]
    sentinel_report = compare_paired(base_s, cand_s)
    ran = len(sentinel) * 2

    # Only an outright regression stops here. A neutral sentinel is expected —
    # eight tasks cannot show a five-point effect — so treating it as failure
    # would discard almost every real improvement.
    if sentinel_report.verdict == VERDICT_REGRESSED:
        sentinel_report.notes.append("stopped at sentinel: regression detected")
        return TieredResult(False, None, sentinel_report, ran)

    base_full = [run_arm(t, False) for t in tasks]
    cand_full = [run_arm(t, True) for t in tasks]
    ran += len(tasks) * 2
    full = compare_paired(base_full, cand_full)
    return TieredResult(True, full, sentinel_report, ran)


def estimate_cost(
    *, task_count: int, candidates: int, tokens_per_task: int, usd_per_million: float
) -> Dict[str, float]:
    """Project replay spend before committing to it.

    Exists because this is the line item most likely to run away: the product of
    four multiplicands, each individually reasonable.
    """
    total_tokens = task_count * 2 * candidates * tokens_per_task
    return {
        "total_tokens": float(total_tokens),
        "usd": round(total_tokens / 1_000_000 * usd_per_million, 4),
        "arms": 2.0,
    }
