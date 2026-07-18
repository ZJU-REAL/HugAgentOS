"""Local deterministic judge for EdgeBench `order_addition_permutation_optimization`.

Original task: find a permutation that minimizes a **black-box cost function** using metaheuristic
search (simulated annealing / genetic / local search), without accessing the cost function's internals.
Metric: score (higher is better).

This judge runs inside the sandbox and is the loop's verify_cmd = ground truth source:

  - Build a deterministic cost function cost(perm) with a fixed seed (here the closed-tour length over
    N cities, i.e. a TSP instance; it is a black box to the contestant — callable only, internals hidden).
  - import the contestant's /workspace/solution.py, which must expose solve(n, cost) -> a permutation of range(n).
  - Verify the return is a valid permutation (exit 0/1 expresses validity, **independent of CPU speed**).
  - SCORE = baseline cost / contestant cost (higher is better); baseline = cost of the identity permutation.

Scoring computes the cost of the final permutation only once → deterministic, unaffected by sandbox
throttling (unlike QPS-style tasks).

Output (for the loop's score_regex / evaluator to parse):
  COST=<result_cost> BASELINE=<identity_cost> SCORE=<ratio>
exit 0 if and only if the return is a valid permutation of range(n). Whether it meets the bar is decided by the loop's target_score.
"""
import math
import sys
import traceback

N = 50
SEED = 20260710


def _fail(msg: str) -> None:
    print("COST=inf BASELINE=0 SCORE=0.0")
    sys.stderr.write(f"JUDGE_ERROR: {msg}\n")
    sys.exit(1)


def _build_instance():
    """Deterministic TSP instance (a black-box cost to the contestant)."""
    import random

    rng = random.Random(SEED)
    pts = [(rng.random(), rng.random()) for _ in range(N)]

    def cost(perm):
        # Total closed-tour length; perm is an ordering of range(N)
        total = 0.0
        for i in range(len(perm)):
            a = pts[perm[i]]
            b = pts[perm[(i + 1) % len(perm)]]
            total += math.hypot(a[0] - b[0], a[1] - b[1])
        return total

    return cost


def main() -> None:
    cost = _build_instance()
    identity_cost = cost(list(range(N)))

    sys.path.insert(0, "/workspace")
    try:
        import solution  # type: ignore
    except Exception:  # noqa: BLE001
        _fail("无法 import /workspace/solution.py（缺失或语法错误）:\n" + traceback.format_exc())

    if not hasattr(solution, "solve"):
        _fail("solution.py 必须提供 solve(n, cost) 函数，返回 range(n) 的一个排列")

    try:
        result = solution.solve(N, cost)
    except Exception:  # noqa: BLE001
        _fail("solution.solve(n, cost) 抛异常:\n" + traceback.format_exc())

    # Verify a valid permutation
    try:
        result = [int(x) for x in result]
    except Exception:  # noqa: BLE001
        _fail("solve 返回值不是整数序列")
    if sorted(result) != list(range(N)):
        _fail(f"solve 返回的不是 range({N}) 的合法排列（每个下标恰好一次）")

    result_cost = cost(result)
    ratio = identity_cost / result_cost if result_cost > 0 else 0.0
    print(f"COST={result_cost:.4f} BASELINE={identity_cost:.4f} SCORE={ratio:.4f}")
    sys.exit(0)


if __name__ == "__main__":
    main()
