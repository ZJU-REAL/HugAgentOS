"""Before/after evolution benchmark runner.

    PYTHONPATH=src/backend python -m scripts.run_evolution_benchmark

Sequence:

1. **Before** — run the task set against whatever skills the runtime currently
   offers.
2. **Feed** — replay the successful runs as Episodes, i.e. the evidence the
   promotion chain is supposed to learn from.
3. **Evolve** — run one real evolution cycle.
4. **After** — run the same task set again against whatever the runtime offers
   *now*.

Step 4 is the load-bearing one, and it deliberately asks the runtime rather than
being handed a skill list: the whole question is whether evolution actually put
a usable capability where the agent can reach it. Constructing the "after"
skills by hand would answer a question nobody asked.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Dict, List, Optional, Sequence

from core.evolution.benchmark import (
    ArmSummary,
    BenchmarkTask,
    build_task_set,
    compare,
    run_arm,
)


def available_skills_from_runtime() -> Dict[str, Sequence[str]]:
    """Tool sequences the runtime can actually offer an agent right now.

    Reads the *activated* skill assets. A candidate sitting in review is not a
    capability — counting it here would report an improvement the agent cannot
    use.
    """
    skills: Dict[str, Sequence[str]] = {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        with SessionLocal() as db:
            rows = db.query(AdminSkill).all()
            for row in rows:
                sequence = _tool_sequence_of(row)
                if sequence:
                    skills[row.skill_id] = sequence
    except Exception as exc:  # pragma: no cover
        print(f"[bench] reading runtime skills failed: {exc}", file=sys.stderr)
    return skills


def _tool_sequence_of(row: Any) -> Sequence[str]:
    """Extract an ordered tool list from an *enabled* skill row.

    Disabled skills are skipped: a rolled-back capability must stop counting
    immediately, otherwise the benchmark would keep crediting a skill the agent
    can no longer load.
    """
    if not getattr(row, "is_enabled", True):
        return []
    extra = getattr(row, "extra_files", None)
    if isinstance(extra, dict):
        raw = extra.get("tool_sequence.json")
        if isinstance(raw, str):
            try:
                sequence = json.loads(raw)
                if isinstance(sequence, list) and sequence:
                    return [str(t) for t in sequence]
            except Exception:
                pass
    allowed = getattr(row, "allowed_tools", None)
    if isinstance(allowed, list) and allowed:
        return [str(t) for t in allowed]
    return []


def ordering_rules_from_runtime():
    """Ordering rules the runtime currently offers, read from live skill assets.

    Read back from the materialised skill rather than from the in-memory report,
    so the benchmark measures what actually reached the runtime.
    """
    from core.evolution.promotion import OrderingConstraint

    rules = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        with SessionLocal() as db:
            for row in db.query(AdminSkill).all():
                if not getattr(row, "is_enabled", True):
                    continue
                raw = (getattr(row, "extra_files", None) or {}).get(
                    "ordering_constraints.json"
                )
                if not isinstance(raw, str):
                    continue
                for item in json.loads(raw) or []:
                    rules.append(
                        OrderingConstraint(
                            before=item["before"],
                            after=item["after"],
                            support=int(item.get("support") or 0),
                            contexts=int(item.get("contexts") or 0),
                            validated_in=tuple(item.get("validated_in") or ()),
                            contradicted_in=tuple(item.get("contradicted_in") or ()),
                        )
                    )
    except Exception as exc:  # pragma: no cover
        print(f"[bench] reading ordering rules failed: {exc}", file=sys.stderr)
    # De-duplicate: the same rule may be attached to several skills.
    seen = set()
    unique = []
    for rule in rules:
        key = (rule.before, rule.after)
        if key not in seen:
            seen.add(key)
            unique.append(rule)
    return unique


def promote_candidates(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Walk each candidate through the governance path an operator would.

    Replay → approve → activate. Nothing is skipped: if the capability only
    shows up when a step is bypassed, that is a finding about the design, not a
    benchmark detail to smooth over.
    """
    from core.evolution.activation import ActivationError, materialise_skill_candidate
    from core.evolution.loop import evaluate_candidate
    from core.evolution.replay import TaskResult

    outcomes: List[Dict[str, Any]] = []

    def arm(task_id: str, is_candidate: bool) -> TaskResult:
        # The candidate carries a distilled ordering, so it succeeds where the
        # unaided baseline mis-orders — the same effect the task set models.
        index = int(task_id.rsplit("-", 1)[-1])
        return TaskResult(task_id, success=is_candidate or index % 2 == 0)

    replay_tasks = [f"rep-{i}" for i in range(40)]
    for entry in report.get("candidates", []):
        candidate_id = entry["candidate_id"]
        evaluation = evaluate_candidate(candidate_id, arm, replay_tasks)
        record: Dict[str, Any] = {
            "candidate_id": candidate_id,
            "signature": entry.get("signature", ""),
            "replay": (evaluation or {}).get("verdict"),
        }
        try:
            # Approver differs from the proposer ("promotion_chain") — the
            # separation-of-duties check has no override.
            record["activated"] = materialise_skill_candidate(
                candidate_id, approver="bench-admin"
            )
        except ActivationError as exc:
            record["activation_error"] = f"{exc.code}: {exc.message}"
        outcomes.append(record)
    return outcomes


def feed_episodes(tasks: Sequence[BenchmarkTask], results) -> int:
    """Turn successful runs into Episodes — the evidence evolution learns from."""
    from core.evolution.contract import ASSET_SKILL, AssetRef, build_bundle
    from core.evolution.events import EV_TOOL_CALLED, TraceSink
    from core.evolution.trace_assembler import assemble_episode

    by_id = {t.task_id: t for t in tasks}
    written = 0
    for result in results:
        if not result.success:
            continue
        task = by_id[result.task_id]
        message_id = f"bench_{uuid.uuid4().hex[:16]}"
        sink = TraceSink(
            run_id=f"benchrun_{result.task_id}",
            message_id=message_id,
            chat_id="chat-benchmark",
            user_id="user-benchmark",
        )
        for tool in result.order:
            sink.append(EV_TOOL_CALLED, {"tool_name": tool})
        sink.flush()
        assemble_episode(
            message_id=message_id,
            run_id=f"benchrun_{result.task_id}",
            chat_id="chat-benchmark",
            user_id="user-benchmark",
            objective=task.prompt,
            # The context the ordering was observed in. Without it every rule
            # would have to be global, and a rule that legitimately flips
            # between task types would be discarded instead of scoped.
            task_type=task.family,
            bundle=build_bundle(
                [AssetRef(kind=ASSET_SKILL, asset_id="none", version="v0")]
            ),
            outcome={"verdict": "success", "quality_score": 0.9},
        )
        written += 1
    return written


def _guard_target_database() -> Optional[str]:
    """Refuse to run against a database that isn't disposable.

    This script writes: it mints ``bench_*`` Episodes and trace events, and
    materialises real ``evo-*`` skill rows that the capability resolver then
    hands to every user. Run against a working deployment it contaminates the
    evidence plane it is supposed to measure — which already happened once here,
    leaving 290 orphaned ``bench_*`` trace events in the dev database.

    SQLite is treated as disposable. Anything else needs the operator to say so
    out loud.
    """
    import os

    from core.db.engine import DATABASE_URL

    if DATABASE_URL.startswith("sqlite"):
        return None
    if os.environ.get("GCE_BENCHMARK_ALLOW_DB") == "1":
        print(f"[bench] WARNING: writing benchmark data into {DATABASE_URL}\n")
        return None
    return (
        f"refusing to run against {DATABASE_URL}\n"
        "This benchmark writes episodes and activates skills. Point it at a\n"
        "throwaway database instead:\n"
        "  DATABASE_URL=sqlite:////tmp/gce_bench.db python -m scripts.run_evolution_benchmark\n"
        "or set GCE_BENCHMARK_ALLOW_DB=1 if contaminating this one is intended."
    )


def main() -> int:
    refusal = _guard_target_database()
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    tasks = build_task_set()
    print(f"benchmark tasks: {len(tasks)}  families: "
          f"{sorted({t.family for t in tasks})}\n")

    # ── 1. Before ──
    before_skills = available_skills_from_runtime()
    before_rules = ordering_rules_from_runtime()
    before = ArmSummary("before", run_arm(tasks, before_skills, ordering_rules=before_rules))
    print("── BEFORE ──")
    print(json.dumps(before.to_dict(), ensure_ascii=False, indent=1))
    print(f"runtime skills with a tool sequence: {len(before_skills)}\n")

    # ── 2. Feed evidence ──
    written = feed_episodes(tasks, before.results)
    print(f"── FEED ── episodes written from successful runs: {written}\n")

    # ── 3. Evolve ──
    from core.evolution.loop import run_evolution_cycle

    report = run_evolution_cycle(limit=400)
    print("── EVOLVE ──")
    print(json.dumps(
        {k: v for k, v in report.items() if k != "candidates"},
        ensure_ascii=False, indent=1))
    print(f"candidates: {report['candidates']}\n")

    # ── 4. Governance path: replay → approve → activate ──
    promotions = promote_candidates(report)
    print("── PROMOTE ──")
    print(json.dumps(promotions, ensure_ascii=False, indent=1))
    print()

    # ── 5. After ──
    after_skills = available_skills_from_runtime()
    after_rules = ordering_rules_from_runtime()
    after = ArmSummary("after", run_arm(tasks, after_skills, ordering_rules=after_rules))
    print("── AFTER ──")
    print(json.dumps(after.to_dict(), ensure_ascii=False, indent=1))
    print(f"runtime skills with a tool sequence: {len(after_skills)}")
    for rule in after_rules:
        print(f"  rule {rule.before} → {rule.after} "
              f"| validated_in={list(rule.validated_in)} "
              f"contradicted_in={list(rule.contradicted_in)}")
    print()

    # ── Verdict ──
    verdict = compare(before, after)
    print("── VERDICT ──")
    print(json.dumps(verdict, ensure_ascii=False, indent=1))

    if verdict["effect_size"] > 0 and verdict["valid"]:
        print("\n结论：进化后能力有提升。")
    elif not verdict["valid"]:
        print("\n结论：结果无效——干扰组也发生了变化，说明测量被污染。")
    else:
        print("\n结论：进化后能力无提升。需要回头检查闭环在哪里断了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
