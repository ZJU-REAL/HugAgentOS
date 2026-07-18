"""Test whether the autonomous loop works using the real EdgeBench task `order_addition_permutation_optimization`.

    docker exec -e LOOP_MAX_ITERS=5 hugagent-backend \
      python /app/src/backend/scripts/_loop_test_edgebench.py

This EdgeBench task: use metaheuristic search to find the permutation that minimizes a black-box cost (score=maximize). Scoring is independent of CPU speed
(the cost is computed once on the final permutation), which suits the rate-limited sandbox environment. It verifies the loop can: iterate → environment verification (ground truth)
→ feed the feedback back in → terminate on meeting verify (or trip the budget fallback).
"""
import asyncio
import json
import os
import pathlib

USER_ID = os.environ.get("LOOP_USER", "copytest_9c0cf31f")
MAX_ITERS = int(os.environ.get("LOOP_MAX_ITERS", "5"))
WORKER_MODEL = os.environ.get("WORKER_MODEL") or None
TARGET = float(os.environ.get("LOOP_TARGET", "1.5"))

JUDGE_SRC = (pathlib.Path(__file__).parent / "edgebench" / "perm_opt_judge.py").read_text("utf-8")

OBJECTIVE = (
    "EdgeBench 任务 order_addition_permutation_optimization：用元启发式搜索找一个使**黑盒成本函数**\n"
    "最小的排列。在沙箱 /workspace 写一个 `solution.py`，暴露：\n"
    "  solve(n, cost) -> list   # 返回 range(n) 的一个排列（每个下标恰好出现一次）\n"
    "  # n 是元素个数；cost 是一个函数：传入一个排列(list)，返回该排列的成本(float，越小越好)。\n"
    "  # cost 是黑盒：只能调用它评估排列，不能看它内部。目标是让 cost(返回值) 尽量小。\n"
    "推荐：模拟退火 / 2-opt 段反转 / 局部搜索，反复调用 cost 迭代改进。\n"
    "评测器运行 `python /workspace/perm_opt_judge.py` 打分：SCORE = 恒等排列成本 / 你的排列成本"
    "（越大越好）。写完后你可以自己先运行它自检并据结果改进。"
)
ACCEPTANCE = [
    "/workspace/solution.py 提供 solve(n, cost) 且返回 range(n) 的合法排列",
    f"SCORE（相对恒等基线的成本改进比）>= {TARGET}",
]
VERIFY_CMD = "cd /workspace && python perm_opt_judge.py"


async def main() -> None:
    from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop, _sbx_exec, _write_file
    from orchestration.loop_evaluator import GoalSpec

    loop_id = "edgebench_permopt"
    session = f"loop-{loop_id}"

    print(f"[test] seeding judge into sandbox session={session} ...", flush=True)
    await _write_file("/workspace/perm_opt_judge.py", JUDGE_SRC, session_id=session, user_id=USER_ID)
    await _sbx_exec("rm -f /workspace/solution.py /workspace/PROGRESS.md /workspace/handoffs.md",
                    session_id=session, user_id=USER_ID)
    pre = await _sbx_exec("ls -la /workspace/perm_opt_judge.py && python --version",
                          session_id=session, user_id=USER_ID, timeout=60)
    print("[test] preflight:", (pre.stdout.strip() if pre else "EXEC_FAILED"), flush=True)

    goal = GoalSpec(
        objective=OBJECTIVE, acceptance_criteria=ACCEPTANCE, verify_cmd=VERIFY_CMD,
        score_regex=r"SCORE=([0-9.]+)", target_score=TARGET, maximize=True,
    )
    budget = LoopBudget(max_iters=MAX_ITERS, max_wall_clock_s=3600.0, max_tokens=5_000_000)

    async def emit(ev):
        t = ev.get("type")
        if t == "iteration_started":
            print(f"\n>>> ITER {ev['seq']} start", flush=True)
        elif t == "loop_tool_call":
            print(f"    · worker tool: {ev.get('name')}", flush=True)
        elif t == "iteration_evaluated":
            print(f"<<< ITER {ev['seq']} verdict={ev['verdict']} score={ev['score']} "
                  f"tools={ev['tool_calls']} tokens={ev['tokens']} by={ev.get('decided_by')}", flush=True)
            print(f"    reason: {ev['reason'][:220]}", flush=True)
            print(f"    evidence: {ev.get('evidence','')[:200]}", flush=True)
        elif t == "loop_completed":
            print(f"\n[loop] COMPLETED status={ev['status']} reason={ev['reason']}", flush=True)

    print(f"[test] START loop max_iters={MAX_ITERS} worker_model={WORKER_MODEL} target={TARGET}", flush=True)
    result = await run_autonomous_loop(
        loop_id=loop_id, user_id=USER_ID, goal_spec=goal, budget=budget,
        model_name=WORKER_MODEL, evaluator_model="fast", worker_max_iters=18,
        session_id=session, emit=emit,
    )

    print("\n" + "=" * 64)
    print("LOOP RESULT:")
    print(json.dumps({
        "status": result.status, "iterations": result.iterations,
        "final_score": result.final_score, "tokens_spent": result.tokens_spent,
        "wall_clock_s": result.wall_clock_s, "reason": result.reason,
        "score_trajectory": [h.get("score") for h in result.history],
        "verdicts": [h.get("verdict") for h in result.history],
    }, ensure_ascii=False, indent=2))
    print("=" * 64)
    effective = result.iterations >= 1 and any(
        h.get("decided_by") == "environment" and h.get("score") is not None for h in result.history
    )
    print(f"\nLOOP_EFFECTIVE={effective}  (≥1 轮由环境验证 ground truth 打分驱动)")
    print(f"LOOP_TERMINATION={result.status}")


if __name__ == "__main__":
    asyncio.run(main())
