"""Before/after benchmark for the evolution loop.

Measures the only question that matters: **does evolution give the runtime a
capability it did not have before?**

Design of the task environment
------------------------------
Each task needs its tools executed in a specific order.  The failure it models
is the one the design document uses as its worked example: an entity check that
runs *after* the data query lets a wrong-entity report through, and the gate
denies it.  Ordering is exactly the kind of knowledge that is expensive to
rediscover each time and cheap to reuse once distilled — which is what the
promotion chain claims to do.

The base model is held fixed and represented as a deterministic planner:

* **without** a matching skill it must derive the order itself, and on tasks
  whose ordering is non-obvious it gets it wrong;
* **with** a matching skill the order arrives pre-assembled.

That is a model, not a live LLM, and it is stated plainly so nobody reads more
into the numbers than they support.  What it isolates is precisely the harness
question — *given the same model, does the system now hold a reusable
capability?* — and it is deterministic, so a difference between arms is
attributable to the harness rather than to sampling.

Deliberate anti-rigging measures
--------------------------------
Two properties keep this from being a benchmark that can only succeed:

1. **Distractor tasks.** Tasks whose ordering is unrelated to any distilled
   skill. If the "after" arm improves on those too, the harness is leaking
   information and the result is invalid.
2. **A trap task.** Its correct order is the *opposite* of the popular one. A
   skill distilled from the majority pattern should make it *worse* — so a
   sweep of uniform improvement is itself a signal that something is wrong.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchmarkTask:
    """One concrete task with a verifiable correct tool ordering."""

    task_id: str
    prompt: str
    required_tools: Tuple[str, ...]
    # The ordering constraint that decides success, e.g. ("company_verify",
    # "db_query") meaning verification must precede the query.
    ordering_constraint: Tuple[str, str]
    family: str = "core"
    # True when the natural/obvious ordering is already correct — the planner
    # gets these right unaided, so evolution cannot help and must not appear to.
    obvious: bool = False

    def is_satisfied_by(self, order: Sequence[str]) -> bool:
        first, second = self.ordering_constraint
        if first not in order or second not in order:
            return False
        return order.index(first) < order.index(second)


def _stable_bit(*parts: str) -> int:
    """Deterministic pseudo-random bit — no RNG, so runs are reproducible."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def build_task_set() -> List[BenchmarkTask]:
    """The benchmark task set: 5 families, 24 tasks."""
    tasks: List[BenchmarkTask] = []

    # ── Family A: industry weekly report (the promotion-chain target) ──
    for i in range(8):
        tasks.append(
            BenchmarkTask(
                task_id=f"report-{i:02d}",
                prompt=f"生成第 {i + 1} 期新能源汽车产业链周报，需核验企业主体并附可追溯来源",
                required_tools=("company_verify", "db_query", "chart_render", "doc_export"),
                ordering_constraint=("company_verify", "db_query"),
                family="industry_report",
            )
        )

    # ── Family B: financial analysis — same ordering lesson, different surface ──
    for i in range(6):
        tasks.append(
            BenchmarkTask(
                task_id=f"finance-{i:02d}",
                prompt=f"分析目标企业 {i + 1} 的财务风险，输出结论前须确认主体一致",
                required_tools=("company_verify", "db_query", "risk_score"),
                ordering_constraint=("company_verify", "db_query"),
                family="finance",
            )
        )

    # ── Family C: distractors — ordering unrelated to any distilled skill ──
    # If these improve too, the harness is leaking and the result is void.
    for i in range(6):
        tasks.append(
            BenchmarkTask(
                task_id=f"kbqa-{i:02d}",
                prompt=f"回答知识库问题 {i + 1}，给出引用",
                required_tools=("kb_search", "cite_check"),
                ordering_constraint=("kb_search", "cite_check"),
                family="kb_qa",
                # Search-then-cite is the obvious order; nothing to learn here.
                obvious=True,
            )
        )

    # ── Family D: the trap — correct order is the reverse of the popular one ──
    # Six of them, so the unaided planner gets some right. That matters: a
    # learned constraint that over-generalises would turn those passes into
    # failures, and the comparison reports regressions explicitly. A trap whose
    # tasks all fail anyway cannot catch over-reach.
    for i in range(6):
        tasks.append(
            BenchmarkTask(
                task_id=f"trap-{i:02d}",
                prompt=f"内部流程草稿 {i + 1}：先取数再核验（该场景下核验依赖查询结果）",
                required_tools=("db_query", "company_verify", "doc_export"),
                ordering_constraint=("db_query", "company_verify"),
                family="trap",
            )
        )

    # ── Family E: long chain, ordering genuinely hard to derive ──
    tasks.append(
        BenchmarkTask(
            task_id="deep-00",
            prompt="跨源尽调：核验主体 → 取数 → 交叉校验 → 出具报告",
            required_tools=("company_verify", "db_query", "cross_check", "doc_export"),
            ordering_constraint=("company_verify", "db_query"),
            family="due_diligence",
        )
    )
    return tasks


@dataclass
class RunResult:
    task_id: str
    family: str
    success: bool
    order: Tuple[str, ...]
    used_skill: Optional[str] = None
    steps: int = 0
    replans: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "family": self.family,
            "success": self.success,
            "used_skill": self.used_skill,
            "steps": self.steps,
            "replans": self.replans,
        }


class FixedPlanner:
    """A deterministic stand-in for the base model's planning.

    Holding it fixed across arms is what makes the comparison about the harness.
    Its one modelled weakness — deriving a non-obvious tool ordering — is the
    weakness the promotion chain claims to remove.
    """

    def __init__(self, *, seed: str = "gce"):
        self.seed = seed

    def plan(
        self,
        task: BenchmarkTask,
        available_skills: Dict[str, Sequence[str]],
        ordering_rules: Optional[Sequence[Any]] = None,
    ) -> Tuple[Tuple[str, ...], Optional[str], int]:
        """Return (tool order, skill used, replans)."""
        # A distilled *sequence* is a memorised instance, so it applies only
        # when the tool set matches exactly.
        #
        # Subset matching was tried first and is wrong: a task needing a subset
        # of a skill's tools is not necessarily a task where that skill's
        # ordering holds. The benchmark's trap family proved it — those tasks
        # need the opposite order, sit inside the popular skill's tool set, and
        # regressed the moment the skill was allowed to claim them. Transfer is
        # the job of the extracted rules below, which carry a conflict veto that
        # a memorised sequence does not.
        for skill_id, sequence in sorted(available_skills.items()):
            if set(task.required_tools) == set(sequence):
                order = tuple(t for t in sequence if t in task.required_tools)
                return order, skill_id, 0

        # No memorised sequence fits. Fall back to unaided planning first, then
        # let any learned rule *correct* the result.
        #
        # Order matters here. An earlier version applied rules to the task's
        # declared tool list directly, which flattered them badly: the declared
        # list already happened to be correctly ordered, so the rule path scored
        # a pass without doing any work. A rule earns credit only by fixing an
        # order the planner actually got wrong.
        proposed, replans = self._unaided(task)

        if ordering_rules:
            from core.evolution.promotion import apply_ordering_constraints

            applicable = [
                r for r in ordering_rules if r.applies_to(proposed, task.family)
            ]
            if applicable:
                corrected = tuple(
                    apply_ordering_constraints(proposed, applicable, task.family)
                )
                if corrected != proposed:
                    return corrected, "rule:" + ",".join(
                        f"{r.before}<{r.after}" for r in applicable
                    ), replans
        return proposed, None, replans

    def _unaided(self, task: BenchmarkTask) -> Tuple[Tuple[str, ...], int]:
        """The base model planning without help.

        Obvious orderings are derived correctly; non-obvious ones are got right
        only about half the time, deterministically.
        """
        if task.obvious:
            return tuple(task.required_tools), 0

        bit = _stable_bit(self.seed, task.task_id)
        if bit % 2 == 0:
            return tuple(task.required_tools), 1

        # The characteristic mistake: the dependency is scheduled too late.
        first, second = task.ordering_constraint
        wrong = [t for t in task.required_tools if t != first]
        insert_at = min(len(wrong), wrong.index(second) + 1) if second in wrong else len(wrong)
        wrong.insert(insert_at, first)
        return tuple(wrong), 2


def run_arm(
    tasks: Sequence[BenchmarkTask],
    available_skills: Dict[str, Sequence[str]],
    *,
    planner: Optional[FixedPlanner] = None,
    ordering_rules: Optional[Sequence[Any]] = None,
) -> List[RunResult]:
    """Run every task under one configuration."""
    planner = planner or FixedPlanner()
    results: List[RunResult] = []
    for task in tasks:
        order, skill, replans = planner.plan(task, available_skills, ordering_rules)
        results.append(
            RunResult(
                task_id=task.task_id,
                family=task.family,
                success=task.is_satisfied_by(order),
                order=order,
                used_skill=skill,
                steps=len(order) + replans,
                replans=replans,
            )
        )
    return results


@dataclass
class ArmSummary:
    name: str
    results: List[RunResult] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return sum(r.success for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def total_steps(self) -> int:
        return sum(r.steps for r in self.results)

    @property
    def total_replans(self) -> int:
        return sum(r.replans for r in self.results)

    def by_family(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for result in self.results:
            bucket = out.setdefault(result.family, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += int(result.success)
        for bucket in out.values():
            bucket["rate"] = round(bucket["passed"] / bucket["total"], 4)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "success_rate": round(self.success_rate, 4),
            "passed": sum(r.success for r in self.results),
            "total": len(self.results),
            "steps": self.total_steps,
            "replans": self.total_replans,
            "by_family": self.by_family(),
        }


def compare(before: ArmSummary, after: ArmSummary) -> Dict[str, Any]:
    """Compare arms, including the checks that can invalidate the result."""
    from core.evolution.replay import mcnemar_p_value

    by_before = {r.task_id: r for r in before.results}
    by_after = {r.task_id: r for r in after.results}
    shared = sorted(set(by_before) & set(by_after))

    b = c = 0
    regressions: List[str] = []
    for task_id in shared:
        was, now = by_before[task_id].success, by_after[task_id].success
        if was and not now:
            b += 1
            regressions.append(task_id)
        elif now and not was:
            c += 1

    # Validity check: distractor families must not move. Improvement there would
    # mean the harness is leaking rather than the capability being real.
    distractor_moved = any(
        by_before[t].success != by_after[t].success
        for t in shared
        if by_before[t].family == "kb_qa"
    )

    return {
        "effect_size": round(after.success_rate - before.success_rate, 4),
        "p_value": round(mcnemar_p_value(b, c), 6),
        "improved_tasks": c,
        "regressed_tasks": b,
        "regressions": regressions,
        "steps_delta": after.total_steps - before.total_steps,
        "replans_delta": after.total_replans - before.total_replans,
        "distractor_contaminated": distractor_moved,
        "valid": not distractor_moved,
    }
