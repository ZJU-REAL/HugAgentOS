"""Deterministic unit check: exit paths of the requirement ledger + read-only review sub-agent (no script verification, no numeric score).

After the refactor, every card the loop flips relies on the driver-spawned read-only review sub-agent
(review_requirement) personally verifying the real output — and done must also pass an independent second
review. This test does not run a real LLM/sandbox/git: it stubs out the worker iteration, requirement
decomposition, reviewer, sandbox read/write and git, feeds only fixed verdicts, and verifies three exits:
all-passed completed / a single requirement exhausting its attempts blocked→budget_exhausted / done rejected
by the second review not flipping the card. Completes in <1s.

Run: docker exec hugagent-backend python -m scripts._loop_convergence_unit
"""
import asyncio

import orchestration.autonomous_loop as al
from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import CONTINUE, DONE, GoalSpec


async def _fake_worker(**kwargs):
    return {"text": "stub work", "tokens": 10, "tool_calls": 1}


def _make_fake_review(verdicts):
    """Return preset verdicts in call order; done items carry non-empty evidence (otherwise the driver downgrades them to continue)."""
    calls = {"i": 0}

    async def _fake_review(**kwargs):
        i = calls["i"]
        calls["i"] += 1
        v = verdicts[min(i, len(verdicts) - 1)]
        return {"verdict": v, "criteria_hit": ["stub"],
                "evidence": "reviewer 读到 /proj/index.html 含目标内容" if v == DONE else "",
                "feedback": "stub 反馈"}

    return _fake_review, calls


def _fake_decompose_n(n):
    async def _fake(**kwargs):
        return [{"id": f"R{i}", "description": f"stub 需求{i}"} for i in range(1, n + 1)]
    return _fake


async def _noop_criteria(**kwargs):
    return ["stub 标准"]


async def _sbx_noop(cmd, **kwargs):
    return (0, "", "")


async def _noop_write_file(path, content, **kwargs):
    return None


async def _noop_read_file(path, **kwargs):
    return ""  # → _read_ledger returns None → fresh init every time


def _patch_common():
    al._run_worker_iteration = _fake_worker
    al._sbx_exec = _sbx_noop
    al._write_file = _noop_write_file
    al._read_file = _noop_read_file
    al.extract_acceptance_criteria = _noop_criteria


async def main() -> None:
    _patch_common()

    # ── Scenario A: 3 requirements, review returns done on the 2nd try for each (continue first), and
    #   all done pass the second review → all-passed completed.
    #   Per requirement: worker→review(continue) in round 1; worker→review(done)+confirm(done) flips in round 2.
    al.decompose_requirements = _fake_decompose_n(3)
    # Sequence (driver calls review in order per requirement; after done it calls confirm):
    #   R1: continue, done, confirm-done → passed (3 calls)
    #   R2: same; R3: same
    seq = []
    for _ in range(3):
        seq += [CONTINUE, DONE, DONE]  # DONE immediately followed by confirm's DONE
    al.review_requirement, _ = _make_fake_review(seq)
    resA = await run_autonomous_loop(
        loop_id="convA", user_id="unit", goal_spec=GoalSpec(objective="stub", acceptance_criteria=["c"]),
        budget=LoopBudget(max_iters=20, max_wall_clock_s=60, max_tokens=10_000_000),
        session_id="loop-convA",
    )
    print(f"[A] status={resA.status} iters={resA.iterations} final={resA.final_score} reason={resA.reason}")
    assert resA.status == "completed", resA.status
    assert resA.final_score == 1.0, resA.final_score

    # ── Scenario B: 1 requirement that always returns continue → attempts exhausted (_MAX_ATTEMPTS_PER_REQ) → blocked → budget_exhausted.
    al.decompose_requirements = _fake_decompose_n(1)
    al.review_requirement, _ = _make_fake_review([CONTINUE])
    resB = await run_autonomous_loop(
        loop_id="convB", user_id="unit", goal_spec=GoalSpec(objective="stub2", acceptance_criteria=["c"]),
        budget=LoopBudget(max_iters=50, max_wall_clock_s=60, max_tokens=10_000_000),
        session_id="loop-convB",
    )
    print(f"[B] status={resB.status} iters={resB.iterations} final={resB.final_score}")
    assert resB.status == "budget_exhausted", resB.status
    assert resB.iterations == al._MAX_ATTEMPTS_PER_REQ, resB.iterations
    assert resB.final_score == 0.0, resB.final_score

    # ── Scenario C: review reports done but the **second review rejects** (confirm=continue) → card not flipped, until attempts are exhausted and blocked.
    al.decompose_requirements = _fake_decompose_n(1)
    #   Each round: review→DONE, confirm→CONTINUE (rejected) → not passed. The sequence alternates.
    al.review_requirement, _ = _make_fake_review([DONE, CONTINUE])
    resC = await run_autonomous_loop(
        loop_id="convC", user_id="unit", goal_spec=GoalSpec(objective="stub3", acceptance_criteria=["c"]),
        budget=LoopBudget(max_iters=50, max_wall_clock_s=60, max_tokens=10_000_000),
        session_id="loop-convC",
    )
    print(f"[C] status={resC.status} iters={resC.iterations}")
    assert resC.status == "budget_exhausted", "done 被二次复核驳回不应翻牌"
    assert resC.iterations == al._MAX_ATTEMPTS_PER_REQ, resC.iterations

    print("CONVERGENCE_UNIT_OK")


if __name__ == "__main__":
    asyncio.run(main())
