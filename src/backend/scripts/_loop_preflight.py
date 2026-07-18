"""Preflight: without going through the worker LLM, directly verifies the judge + verify pipeline + the evaluator's done/continue decision."""
import asyncio
import os
import pathlib

USER_ID = os.environ.get("LOOP_USER", "copytest_9c0cf31f")
JUDGE_SRC = (pathlib.Path(__file__).parent / "edgebench" / "perm_opt_judge.py").read_text("utf-8")
TARGET = 1.5

# Reference solution: black-box 2-opt local search (only calls cost, does not look inside) — should reliably reach ratio >= 1.5
GOOD_SOLUTION = '''
import random, math
def solve(n, cost):
    rng = random.Random(0)
    cur = list(range(n)); cur_c = cost(cur)
    best, best_c = cur[:], cur_c
    T = 0.5
    for _ in range(6000):
        a, b = sorted((rng.randrange(n), rng.randrange(n)))
        if a == b: continue
        cand = cur[:]; cand[a:b+1] = reversed(cand[a:b+1])
        c = cost(cand)
        if c < cur_c or rng.random() < math.exp((cur_c - c) / max(T, 1e-9)):
            cur, cur_c = cand, c
            if c < best_c: best, best_c = cand[:], c
        T *= 0.999
    return best
'''

BAD_SOLUTION = '''
def solve(n, cost):
    return list(range(n))   # identity permutation, not optimized → score≈1.0 < target
'''


async def main():
    from orchestration.autonomous_loop import _sbx_exec, _write_file
    from orchestration.loop_evaluator import GoalSpec, evaluate_iteration

    session = "loop-preflight2"
    verify = "cd /workspace && python perm_opt_judge.py"
    await _write_file("/workspace/perm_opt_judge.py", JUDGE_SRC, session_id=session, user_id=USER_ID)

    goal = GoalSpec(
        objective="permutation optimization", acceptance_criteria=["合法排列", f"score>={TARGET}"],
        verify_cmd=verify, score_regex=r"SCORE=([0-9.]+)", target_score=TARGET, maximize=True,
    )

    async def run_case(label, sol):
        await _sbx_exec("rm -f /workspace/solution.py", session_id=session, user_id=USER_ID)
        if sol:
            await _write_file("/workspace/solution.py", sol, session_id=session, user_id=USER_ID)
        r = await _sbx_exec(verify, session_id=session, user_id=USER_ID, timeout=120)
        print(f"{label}: exit={r.exit_code if r else '?'} out={(r.stdout.strip() if r else '')}")
        v = await evaluate_iteration(goal_spec=goal, session_id=session, user_id=USER_ID,
                                     iteration_summary="", use_llm_for_fuzzy=False)
        print(f"   evaluator verdict={v['verdict']} score={v['score']} by={v['decided_by']}")
        return v

    print("=== A) 无解 → 期望 continue ===")
    va = await run_case("A missing", None)
    print("=== B) 未优化(恒等) → 期望 continue(exit0 但 score<target) ===")
    vb = await run_case("B identity", BAD_SOLUTION)
    print("=== C) 优化解 → 期望 done ===")
    vc = await run_case("C optimized", GOOD_SOLUTION)

    ok = (va["verdict"] == "continue" and vb["verdict"] == "continue"
          and vc["verdict"] == "done" and vc["score"] and vc["score"] >= TARGET)
    print("\nPREFLIGHT_OK" if ok else "PREFLIGHT_FAIL")


if __name__ == "__main__":
    asyncio.run(main())
