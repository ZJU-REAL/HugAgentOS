"""self_verify (conversation mode) smoke test: no verify_cmd given, the worker builds /workspace/verify.sh itself.

Verification points:
  - driver supplies self_verify with a default verify_cmd / score_regex;
  - the worker builds verify.sh each iteration, and the driver independently produces a score;
  - with no threshold it will not call done on the first valid solution, converging via stagnation (or budget).

Run (inside container):
  docker exec hugagent-backend python -m scripts._loop_selfverify_smoke
"""
import asyncio
import uuid

from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
from orchestration.loop_evaluator import GoalSpec


async def main() -> None:
    loop_id = "sv_" + uuid.uuid4().hex[:8]
    gs = GoalSpec(
        objective=(
            "在 /workspace/solution.py 写一个函数 nth_prime(n) 返回第 n 个素数（n 从 1 起，"
            "nth_prime(1)==2）。尽量正确、边界稳妥。"
        ),
        mode="self_verify",  # key point: conversation mode, no verify_cmd
    )
    budget = LoopBudget(max_iters=6, max_wall_clock_s=1200, max_tokens=1_500_000)

    events = []

    async def emit(ev):
        events.append(ev)
        t = ev.get("type")
        if t in ("iteration_started", "iteration_evaluated", "loop_converged",
                 "loop_stagnation", "loop_completed"):
            print(f"  · {t}: "
                  f"seq={ev.get('seq')} verdict={ev.get('verdict')} "
                  f"score={ev.get('score')} best={ev.get('best_score')} "
                  f"status={ev.get('status')} reason={str(ev.get('reason',''))[:80]}")

    print(f"[selfverify] start loop {loop_id}")
    result = await run_autonomous_loop(
        loop_id=loop_id,
        user_id="svsmoke01",
        goal_spec=gs,
        budget=budget,
        model_name=None,
        evaluator_model="fast",
        worker_max_iters=12,
        session_id=f"loop-{loop_id}",
        emit=emit,
    )
    print(f"\n[selfverify] RESULT status={result.status} iters={result.iterations} "
          f"final_score={result.final_score} reason={result.reason}")
    # driver should have supplied self_verify with the default script convention
    assert gs.verify_cmd and "verify.sh" in gs.verify_cmd, gs.verify_cmd
    assert gs.score_regex, "score_regex 未补默认"
    # must have produced at least one valid solution (with a score) or exhausted budget -- never 0 iterations
    assert result.iterations >= 1
    scored = [h for h in result.history if h.get("score") is not None]
    print(f"[selfverify] scored_iters={len(scored)} scores={[h['score'] for h in scored]}")
    print("SELFVERIFY_OK" if result.status in ("completed", "budget_exhausted") else "SELFVERIFY_UNEXPECTED")


if __name__ == "__main__":
    asyncio.run(main())
